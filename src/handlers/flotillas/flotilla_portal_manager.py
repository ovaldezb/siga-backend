"""Portal público de flotilla — el cliente flotillero consulta el historial de
servicio de sus unidades (360° versión cliente) sin cuenta en el sistema.

MODELO DE ACCESO
----------------
Mismo patrón que `ordenes/cliente_link_manager.py` (token HMAC + nonce en Mongo),
con tres diferencias deliberadas:

1. El secreto es un PIN de 6 dígitos generado por el sistema, NO un challenge
   derivado de placa+teléfono: una flotilla agrupa decenas de unidades y varios
   contactos, así que no hay un dato único del que derivarlo.
2. El token vive 180 días (no 14): es un portal recurrente, no un one-shot para
   aprobar una cotización. Se rota o revoca desde la ficha de la flotilla.
3. La sesión dura 8 horas (no 30 min): el flotillero navega un historial largo.

ES UN PORTAL DE SOLO LECTURA. No hay ni un handler que escriba datos de negocio;
lo único que se persiste es la bitácora de acceso (intentos, último acceso).

AISLAMIENTO
-----------
- El tenant sale del token firmado, nunca de la request → no hay forma de pedir
  datos de otro taller.
- El alcance de la flotilla se resuelve SIEMPRE desde `clientes.flotilla_id`; toda
  unidad y toda OS se valida contra ese conjunto antes de devolverse (si no, el
  `vehiculo_id` de la query sería un IDOR sobre el tenant completo).
- Los importes que se exponen son de venta (`precioVenta`, `subtotal`, `total`).
  Nunca sale `precioCompra`, `costo_proveedor`, margen ni nota interna, y los
  items `no_cobrar` (cortesías) se filtran antes de salir del backend.
"""

import hmac
import json
import os
import secrets
from datetime import datetime, timedelta
from typing import Optional, Tuple

from aws_lambda_powertools import Logger

from src.shared.infrastructure.database import get_tenant_db
from src.shared.utils.auth_utils import get_claims, parse_object_id
from src.shared.utils.date_utils import iso_utc
from src.shared.utils.indexes import ensure_indexes
from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.utils import public_link

logger = Logger()

PORTAL_TOKEN_TTL_DAYS = 180
SESSION_TTL_HOURS = 8
MAX_VERIFY_ATTEMPTS = 5
PIN_LENGTH = 6

# Topes de lectura: una flotilla grande con años de historia no debe tumbar la
# Lambda ni devolver un payload de megas al celular del cliente.
MAX_UNIDADES = 500
MAX_OS_POR_UNIDAD = 100
MAX_DOCS_ESTADO_CUENTA = 200

ESTADOS_ABIERTOS = ["RECEPCION", "COTIZADO", "APROBADO", "EN_PROCESO"]
ESTADOS_CERRADOS = ["FINALIZADO", "ENTREGADO"]
# Una OS cancelada se sigue mostrando en el historial (pasó y el cliente puede
# preguntar por ella), pero no cuenta como servicio ni suma al gasto.
ESTADOS_CANCELADOS = ["CANCELADO", "CANCELADA"]
VENTAS_NO_VIGENTES = ["CANCELADA", "ANULADA"]
# Citas que al flotillero todavía le sirven de aviso (las demás ya son historia).
CITAS_VIGENTES = ["pendiente", "confirmada", "en_proceso"]

# Campos de item visibles al flotillero. Todo lo que no esté aquí se filtra
# (precioCompra, costo_proveedor, proveedor_id, notas internas, linea_id, etc.).
_PUBLIC_ITEM_FIELDS = {
    'nombre', 'descripcion', 'noParte', 'marca',
    'piezas', 'precioVenta', 'subtotal', 'tipo',
}


# ---------- helpers de acceso ----------

def _build_portal_url(token: str) -> str:
    base = (os.environ.get('FLOTILLA_PORTAL_BASE_URL') or '').strip().rstrip('/')
    if not base:
        app = (os.environ.get('APP_URL') or '').strip().rstrip('/')
        if app:
            # El SPA usa hash routing; la ruta pública es `flotilla/portal/:token`.
            base = f'{app}/#/flotilla/portal'
    if not base:
        base = 'https://app.example.com/#/flotilla/portal'
    return f'{base}/{token}'


def _generar_pin() -> str:
    """PIN numérico de PIN_LENGTH dígitos, uniformemente aleatorio (puede iniciar en 0)."""
    return ''.join(secrets.choice('0123456789') for _ in range(PIN_LENGTH))


def _tenant_desde_claims(event) -> Tuple[Optional[str], Optional[dict]]:
    claims = get_claims(event)
    tenant_id = claims.get('custom:tenant_id')
    if not tenant_id:
        return None, create_response(403, 'No se encontró un tenantId asociado.')
    return tenant_id, None


def _sesion_valida(db, payload: dict) -> Tuple[Optional[dict], Optional[dict]]:
    """Valida que el session_token corresponda al nonce vigente. (acceso, error_response)."""
    flotilla_id = payload.get('f')
    acceso = db['flotilla_acceso'].find_one({'flotilla_id': flotilla_id})
    if not acceso or acceso.get('nonce') != payload.get('n'):
        return None, create_response(401, 'Sesión inválida. Vuelve a entrar con tu PIN.')
    if acceso.get('bloqueado_en'):
        return None, create_response(403, 'Acceso bloqueado. Pide a tu taller un enlace nuevo.')
    return acceso, None


def _abrir_sesion(event) -> Tuple[Optional[dict], Optional[str], Optional[str], Optional[dict]]:
    """Resuelve session_token → (db, tenant_id, flotilla_id, error_response)."""
    qp = event.get('queryStringParameters') or {}
    payload = public_link.verify(qp.get('session_token', ''))
    if not payload:
        return None, None, None, create_response(401, 'Sesión expirada o inválida.')
    if payload.get('s') != 1:
        return None, None, None, create_response(401, 'Se requiere session_token (no token de acceso).')

    tenant_id = payload.get('t')
    flotilla_id = payload.get('f')
    if not tenant_id or not flotilla_id:
        return None, None, None, create_response(401, 'Token incompleto.')

    db = get_tenant_db(tenant_id)
    _, err = _sesion_valida(db, payload)
    if err:
        return None, None, None, err
    return db, tenant_id, flotilla_id, None


# ---------- alcance de la flotilla ----------

def _scope_flotilla(db, flotilla_id: str) -> dict:
    """Universo de datos de la flotilla: clientes miembros y sus unidades.

    Es la única fuente de autorización del portal: cualquier id que llegue por
    query se valida contra estos conjuntos.
    """
    clientes = list(db['clientes'].find(
        {'flotilla_id': flotilla_id},
        {'nombre': 1, 'apellido_paterno': 1, 'apellido_materno': 1, 'razon_social': 1},
    ))
    cliente_ids = []
    cliente_nombres = {}
    for c in clientes:
        cid = str(c['_id'])
        cliente_ids.append(cid)
        cliente_nombres[cid] = (
            c.get('razon_social')
            or ' '.join(filter(None, [
                c.get('nombre'), c.get('apellido_paterno'), c.get('apellido_materno')
            ])).strip()
            or 'Cliente'
        )

    vehiculos = []
    if cliente_ids:
        vehiculos = list(db['vehiculos'].find(
            {'cliente_id': {'$in': cliente_ids}},
            {
                'placas': 1, 'marca': 1, 'modelo': 1, 'anio': 1, 'color': 1,
                'vin': 1, 'kilometraje': 1, 'cliente_id': 1,
                'proximo_cambio_aceite': 1, 'proximo_cambio_bujias': 1,
                'proximo_cambio_aceite_fecha': 1, 'proximo_cambio_bujias_fecha': 1,
            },
        ).limit(MAX_UNIDADES))

    return {
        'cliente_ids': cliente_ids,
        'cliente_nombres': cliente_nombres,
        'vehiculos': vehiculos,
        'vehiculo_ids': [str(v['_id']) for v in vehiculos],
    }


def _ordenes_de_flotilla(db, scope: dict, projection: Optional[dict], vehiculo_id: Optional[str] = None):
    """OS de la flotilla. Empareja por vehículo Y por cliente: hay OS históricas
    cuyo `vehiculo_id` quedó vacío, y unidades que cambiaron de dueño dentro del
    mismo corporativo."""
    veh_ids = [vehiculo_id] if vehiculo_id else scope['vehiculo_ids']
    if not veh_ids and not scope['cliente_ids']:
        return []

    ors = []
    if veh_ids:
        ors.append({'vehiculo_id': {'$in': veh_ids}})
    if scope['cliente_ids'] and not vehiculo_id:
        ors.append({'cliente_snapshot.id': {'$in': scope['cliente_ids']}})
    query = ors[0] if len(ors) == 1 else {'$or': ors}
    return list(db['ordenes_servicio'].find(query, projection))


def _fecha_doc(doc: dict) -> str:
    """`createdAt` como ISO string. Mongo lo guarda como datetime en OS/ventas y
    como string en documentos escritos por handlers ya migrados a `iso_utc`."""
    f = doc.get('createdAt')
    if isinstance(f, datetime):
        return iso_utc(f)
    return f or ''


# ---------- sanitización ----------

def _sanitizar_items(orden: dict) -> list:
    """Puntos/items visibles al cliente.

    Excluye exactamente lo que `_calcular_totales_orden` excluye del total
    (cortesías y rechazados), para que la suma de lo que ve el cliente cuadre
    con el `total` que se le muestra.
    """
    puntos_pub = []
    for punto in orden.get('puntosArreglar') or []:
        items_pub = []
        for item in punto.get('items') or []:
            if item.get('no_cobrar'):
                continue
            if item.get('rechazado') or item.get('decision') == 'rechazado':
                continue
            filt = {k: v for k, v in item.items() if k in _PUBLIC_ITEM_FIELDS}
            filt['estado'] = 'aprobado' if (
                item.get('decision') == 'aprobado' or item.get('aprobado') is True
            ) else 'pendiente'
            items_pub.append(filt)
        if items_pub:
            puntos_pub.append({'nombre': punto.get('nombre'), 'items': items_pub})
    return puntos_pub


def _sanitizar_os_detalle(orden: dict, cliente_nombres: dict) -> dict:
    cliente_id = (orden.get('cliente_snapshot') or {}).get('id')
    return {
        'id': str(orden['_id']),
        'folio': orden.get('folio'),
        'estado': orden.get('estado'),
        'fecha': _fecha_doc(orden),
        'fecha_estimada_entrega': orden.get('fechaEstimadaEntrega'),
        'kilometraje': orden.get('kilometraje', 0),
        'falla_reportada': orden.get('falla_reportada'),
        'diagnostico': orden.get('diagnostico'),
        'total': float(orden.get('total') or 0),
        'anticipo': float(orden.get('anticipo') or 0),
        'titular': cliente_nombres.get(cliente_id, ''),
        'proximo_cambio_aceite': orden.get('proximo_cambio_aceite') or 0,
        'proximo_cambio_bujias': orden.get('proximo_cambio_bujias') or 0,
        'proximo_cambio_aceite_fecha': orden.get('proximo_cambio_aceite_fecha') or '',
        'proximo_cambio_bujias_fecha': orden.get('proximo_cambio_bujias_fecha') or '',
        'puntos': _sanitizar_items(orden),
    }


# ---------- handlers internos (Cognito) ----------

@logger.inject_lambda_context
def create_portal_link_handler(event, context):
    """POST /flotillas/{id}/portal-link — Genera o rota el enlace del portal.

    Rotar invalida el enlace anterior y emite un PIN nuevo: es la palanca para
    cortar acceso cuando cambia el responsable de la flota del cliente.
    """
    try:
        tenant_id, err = _tenant_desde_claims(event)
        if err:
            return err
        claims = get_claims(event)

        flotilla_id = event['pathParameters']['id']
        oid, perr = parse_object_id(flotilla_id)
        if perr:
            return create_response(400, perr)

        db = get_tenant_db(tenant_id)
        ensure_indexes(db, tenant_id)

        flotilla = db['flotillas'].find_one({'_id': oid}, {'nombre': 1})
        if not flotilla:
            return create_response(404, 'Flotilla no encontrada.')

        if db['clientes'].count_documents({'flotilla_id': flotilla_id}, limit=1) == 0:
            return create_response(
                400,
                'La flotilla no tiene clientes asignados; el portal no tendría unidades que mostrar.'
            )

        body = json.loads(event.get('body') or '{}')
        nonce = secrets.token_hex(16)
        pin = _generar_pin()
        exp_dt = datetime.utcnow() + timedelta(days=PORTAL_TOKEN_TTL_DAYS)

        db['flotilla_acceso'].update_one(
            {'flotilla_id': flotilla_id},
            {'$set': {
                'flotilla_id': flotilla_id,
                'nonce': nonce,
                # Verificación contra el hash; el plano es para que el asesor pueda
                # volver a dictarle el PIN al cliente sin invalidar el enlace. Vive
                # en la misma DB del tenant que ya contiene todo su negocio, así que
                # no amplía la superficie en caso de compromiso.
                'pin_hash': public_link.hash_answer(pin),
                'pin': pin,
                'contacto_nombre': (body.get('contacto_nombre') or '').strip(),
                'created_at': iso_utc(),
                'created_by': claims.get('email') or 'system',
                'expires_at': iso_utc(exp_dt),
                'intentos_verificacion': 0,
                'bloqueado_en': None,
                'ultimo_acceso': None,
                'num_accesos': 0,
            }},
            upsert=True,
        )

        token = public_link.sign({
            't': tenant_id,
            'f': flotilla_id,
            'n': nonce,
            'exp': int(exp_dt.timestamp()),
        })

        return create_response(200, 'Portal generado', {
            'url': _build_portal_url(token),
            'token': token,
            'pin': pin,  # solo el asesor lo ve; se comparte por WhatsApp/teléfono
            'expires_at': iso_utc(exp_dt),
            'flotilla_nombre': flotilla.get('nombre'),
        })
    except Exception as e:
        return handle_exception(e)


@logger.inject_lambda_context
def get_portal_link_handler(event, context):
    """GET /flotillas/{id}/portal-link — Estado del portal vigente, sin rotarlo."""
    try:
        tenant_id, err = _tenant_desde_claims(event)
        if err:
            return err

        flotilla_id = event['pathParameters']['id']
        db = get_tenant_db(tenant_id)

        acceso = db['flotilla_acceso'].find_one({'flotilla_id': flotilla_id})
        if not acceso:
            return create_response(404, 'Esta flotilla no tiene portal activo.')

        try:
            exp_dt = datetime.fromisoformat((acceso.get('expires_at') or '').rstrip('Z'))
        except ValueError:
            exp_dt = datetime.utcnow() + timedelta(days=PORTAL_TOKEN_TTL_DAYS)

        token = public_link.sign({
            't': tenant_id,
            'f': flotilla_id,
            'n': acceso['nonce'],
            'exp': int(exp_dt.timestamp()),
        })

        return create_response(200, 'Portal recuperado', {
            'url': _build_portal_url(token),
            'token': token,
            'pin': acceso.get('pin'),
            'contacto_nombre': acceso.get('contacto_nombre'),
            'expires_at': acceso.get('expires_at'),
            'expirado': datetime.utcnow() > exp_dt,
            'created_at': acceso.get('created_at'),
            'created_by': acceso.get('created_by'),
            'intentos_verificacion': acceso.get('intentos_verificacion', 0),
            'bloqueado_en': acceso.get('bloqueado_en'),
            'ultimo_acceso': acceso.get('ultimo_acceso'),
            'num_accesos': acceso.get('num_accesos', 0),
        })
    except Exception as e:
        return handle_exception(e)


@logger.inject_lambda_context
def revoke_portal_link_handler(event, context):
    """DELETE /flotillas/{id}/portal-link — Revoca el acceso del portal."""
    try:
        tenant_id, err = _tenant_desde_claims(event)
        if err:
            return err

        flotilla_id = event['pathParameters']['id']
        db = get_tenant_db(tenant_id)
        result = db['flotilla_acceso'].delete_one({'flotilla_id': flotilla_id})
        if result.deleted_count == 0:
            return create_response(404, 'Esta flotilla no tenía portal activo.')
        return create_response(200, 'Portal revocado')
    except Exception as e:
        return handle_exception(e)


# ---------- handlers públicos (SIN authorizer) ----------

@logger.inject_lambda_context
def public_challenge_handler(event, context):
    """GET /public/flotilla/challenge?token=... — Qué pedirle al visitante."""
    try:
        qp = event.get('queryStringParameters') or {}
        payload = public_link.verify(qp.get('token', ''))
        if not payload:
            return create_response(401, 'Enlace inválido o expirado. Pide uno nuevo a tu taller.')
        if payload.get('s') == 1:
            return create_response(401, 'Se requiere el token del enlace, no una sesión.')

        tenant_id = payload.get('t')
        flotilla_id = payload.get('f')
        if not tenant_id or not flotilla_id:
            return create_response(401, 'Token incompleto.')

        db = get_tenant_db(tenant_id)
        acceso = db['flotilla_acceso'].find_one({'flotilla_id': flotilla_id})
        if not acceso or acceso.get('nonce') != payload.get('n'):
            return create_response(401, 'Este enlace ya no es válido (fue regenerado o revocado).')
        if acceso.get('bloqueado_en'):
            return create_response(403, 'Demasiados intentos. Pide a tu taller un enlace nuevo.')

        oid, perr = parse_object_id(flotilla_id)
        flotilla = db['flotillas'].find_one({'_id': oid}, {'nombre': 1}) if not perr else None

        return create_response(200, 'Challenge', {
            'flotilla_nombre': (flotilla or {}).get('nombre') or 'Tu flota',
            'prompt': f'Ingresa el PIN de {PIN_LENGTH} dígitos que te compartió el taller',
            'pin_length': PIN_LENGTH,
            'intentos_restantes': max(0, MAX_VERIFY_ATTEMPTS - acceso.get('intentos_verificacion', 0)),
        })
    except Exception as e:
        return handle_exception(e)


@logger.inject_lambda_context
def public_verify_handler(event, context):
    """POST /public/flotilla/verify  Body: {token, pin} → session_token."""
    try:
        body = json.loads(event.get('body') or '{}')
        payload = public_link.verify(body.get('token', ''))
        if not payload:
            return create_response(401, 'Enlace inválido o expirado. Pide uno nuevo a tu taller.')
        if payload.get('s') == 1:
            return create_response(401, 'Se requiere el token del enlace, no una sesión.')

        tenant_id = payload.get('t')
        flotilla_id = payload.get('f')
        if not tenant_id or not flotilla_id:
            return create_response(401, 'Token incompleto.')

        raw = body.get('pin', '')
        pin = public_link.digits(''.join(str(x) for x in raw) if isinstance(raw, list) else str(raw))

        db = get_tenant_db(tenant_id)
        acceso = db['flotilla_acceso'].find_one({'flotilla_id': flotilla_id})
        if not acceso or acceso.get('nonce') != payload.get('n'):
            return create_response(401, 'Este enlace ya no es válido (fue regenerado o revocado).')
        if acceso.get('bloqueado_en'):
            return create_response(403, 'Demasiados intentos. Pide a tu taller un enlace nuevo.')

        esperado = acceso.get('pin_hash') or ''
        # compare_digest: comparación en tiempo constante (el PIN es de 6 dígitos).
        if not esperado or not hmac.compare_digest(public_link.hash_answer(pin), esperado):
            intentos = acceso.get('intentos_verificacion', 0) + 1
            update = {'intentos_verificacion': intentos}
            if intentos >= MAX_VERIFY_ATTEMPTS:
                update['bloqueado_en'] = iso_utc()
            db['flotilla_acceso'].update_one({'flotilla_id': flotilla_id}, {'$set': update})
            restantes = max(0, MAX_VERIFY_ATTEMPTS - intentos)
            return create_response(401, f'PIN incorrecto. Te quedan {restantes} intentos.',
                                   {'intentos_restantes': restantes})

        session_exp = datetime.utcnow() + timedelta(hours=SESSION_TTL_HOURS)
        session_token = public_link.sign({
            't': tenant_id,
            'f': flotilla_id,
            'n': acceso['nonce'],
            's': 1,
            'exp': int(session_exp.timestamp()),
        })

        db['flotilla_acceso'].update_one(
            {'flotilla_id': flotilla_id},
            {
                '$set': {
                    'intentos_verificacion': 0,
                    'ultimo_acceso': iso_utc(),
                    'ultimo_acceso_meta': {
                        'ip': public_link.client_ip(event),
                        'user_agent': public_link.user_agent(event),
                    },
                },
                '$inc': {'num_accesos': 1},
            },
        )

        return create_response(200, 'Acceso concedido', {
            'session_token': session_token,
            'expires_at': iso_utc(session_exp),
        })
    except Exception as e:
        return handle_exception(e)


@logger.inject_lambda_context
def public_resumen_handler(event, context):
    """GET /public/flotilla?session_token=... — Tablero de la flota (solo lectura).

    Devuelve KPIs, la lista de unidades con su último servicio y el estado de
    cuenta (saldos por cobrar + OS cerradas aún no facturadas).
    """
    try:
        db, _tenant_id, flotilla_id, err = _abrir_sesion(event)
        if err:
            return err

        oid, perr = parse_object_id(flotilla_id)
        flotilla = db['flotillas'].find_one(
            {'_id': oid}, {'nombre': 1, 'razon_social': 1}
        ) if not perr else None

        scope = _scope_flotilla(db, flotilla_id)

        # --- OS: último servicio por unidad + conteos. Proyección sin puntosArreglar:
        # el listado no necesita el detalle y así el payload no crece con la historia.
        ordenes = _ordenes_de_flotilla(db, scope, {
            'folio': 1, 'estado': 1, 'createdAt': 1, 'kilometraje': 1,
            'total': 1, 'vehiculo_id': 1, 'cliente_snapshot.id': 1, 'facturada': 1,
        })
        ordenes.sort(key=_fecha_doc, reverse=True)

        ultima_por_veh = {}
        os_por_veh = {}
        abiertas_por_veh = {}
        os_abiertas = 0
        servicios_realizados = 0
        for o in ordenes:
            vid = o.get('vehiculo_id')
            cancelada = o.get('estado') in ESTADOS_CANCELADOS
            if not cancelada:
                servicios_realizados += 1
                if vid:
                    os_por_veh[vid] = os_por_veh.get(vid, 0) + 1
                    if vid not in ultima_por_veh:
                        ultima_por_veh[vid] = o
            if o.get('estado') in ESTADOS_ABIERTOS:
                os_abiertas += 1
                if vid:
                    abiertas_por_veh[vid] = abiertas_por_veh.get(vid, 0) + 1

        unidades = []
        for v in scope['vehiculos']:
            vid = str(v['_id'])
            ultima = ultima_por_veh.get(vid)
            unidades.append({
                'id': vid,
                'placas': v.get('placas') or '',
                'marca': v.get('marca'),
                'modelo': v.get('modelo'),
                'anio': v.get('anio'),
                'color': v.get('color'),
                'titular': scope['cliente_nombres'].get(v.get('cliente_id'), ''),
                'kilometraje': v.get('kilometraje') or 0,
                'proximo_cambio_aceite': v.get('proximo_cambio_aceite') or 0,
                'proximo_cambio_bujias': v.get('proximo_cambio_bujias') or 0,
                'proximo_cambio_aceite_fecha': v.get('proximo_cambio_aceite_fecha') or '',
                'proximo_cambio_bujias_fecha': v.get('proximo_cambio_bujias_fecha') or '',
                'num_servicios': os_por_veh.get(vid, 0),
                'os_abiertas': abiertas_por_veh.get(vid, 0),
                'ultimo_servicio': {
                    'folio': ultima.get('folio'),
                    'fecha': _fecha_doc(ultima),
                    'estado': ultima.get('estado'),
                    'kilometraje': ultima.get('kilometraje', 0),
                    'total': float(ultima.get('total') or 0),
                } if ultima else None,
            })
        unidades.sort(key=lambda u: (u['placas'] or 'zzz', u['marca'] or ''))

        # --- Estado de cuenta: ventas con saldo pendiente + gasto histórico.
        saldo_total = 0.0
        gasto_total = 0.0
        documentos = []
        if scope['cliente_ids']:
            base_ventas = {
                'cliente_id': {'$in': scope['cliente_ids']},
                'estado': {'$nin': VENTAS_NO_VIGENTES},
            }
            # El gasto acumulado se agrega en Mongo, no sumando la página de
            # documentos: con el tope de MAX_DOCS_ESTADO_CUENTA el KPI saldría corto
            # justo para las flotas grandes, que son las que lo miran.
            agg = list(db['ventas'].aggregate([
                {'$match': base_ventas},
                {'$group': {'_id': None, 'gasto': {'$sum': {'$ifNull': ['$total', 0]}}}},
            ]))
            if agg:
                gasto_total = float(agg[0].get('gasto') or 0)

            # Cartera: sólo documentos que todavía deben algo (índice parcial
            # ventas_saldo_pendiente).
            ventas = list(db['ventas'].find(
                {**base_ventas, 'saldo_pendiente': {'$gt': 0}},
                {'folio': 1, 'createdAt': 1, 'total': 1, 'saldo_pendiente': 1,
                 'cliente_id': 1, 'orden_id': 1},
            ).sort('createdAt', -1).limit(MAX_DOCS_ESTADO_CUENTA))
            for v in ventas:
                saldo = float(v.get('saldo_pendiente') or 0)
                saldo_total += saldo
                documentos.append({
                    'folio': v.get('folio'),
                    'fecha': _fecha_doc(v),
                    'total': float(v.get('total') or 0),
                    'saldo_pendiente': round(saldo, 2),
                    'titular': scope['cliente_nombres'].get(v.get('cliente_id'), ''),
                    'orden_id': v.get('orden_id'),
                })

        # Servicios terminados que el taller aún no factura: el flotillero los usa
        # para saber qué CFDI está esperando.
        sin_facturar = [
            {'folio': o.get('folio'), 'fecha': _fecha_doc(o), 'total': float(o.get('total') or 0)}
            for o in ordenes
            if o.get('estado') in ESTADOS_CERRADOS and not o.get('facturada')
        ][:MAX_DOCS_ESTADO_CUENTA]

        return create_response(200, 'Resumen de flota', {
            'flotilla': {
                'nombre': (flotilla or {}).get('nombre') or 'Tu flota',
                'razon_social': (flotilla or {}).get('razon_social') or '',
            },
            'kpis': {
                'unidades': len(unidades),
                'servicios_historicos': servicios_realizados,
                'os_abiertas': os_abiertas,
                'gasto_total': round(gasto_total, 2),
                'saldo_pendiente': round(saldo_total, 2),
            },
            'unidades': unidades,
            'estado_cuenta': {
                'saldo_total': round(saldo_total, 2),
                'documentos': documentos,
                'os_sin_facturar': sin_facturar,
            },
            'generado_en': iso_utc(),
        })
    except Exception as e:
        return handle_exception(e)


@logger.inject_lambda_context
def public_vehiculo_handler(event, context):
    """GET /public/flotilla/vehiculo?session_token=...&vehiculo_id=... — 360° de una unidad.

    El `vehiculo_id` se valida contra las unidades de la flotilla: sin esa
    comprobación el parámetro sería un IDOR sobre los vehículos del tenant.
    """
    try:
        db, _tenant_id, flotilla_id, err = _abrir_sesion(event)
        if err:
            return err

        qp = event.get('queryStringParameters') or {}
        vehiculo_id = (qp.get('vehiculo_id') or '').strip()
        if not vehiculo_id:
            return create_response(400, 'Falta vehiculo_id.')

        scope = _scope_flotilla(db, flotilla_id)
        if vehiculo_id not in scope['vehiculo_ids']:
            # Mismo mensaje que "no existe": no confirmamos la existencia de
            # unidades ajenas a la flotilla.
            return create_response(404, 'Unidad no encontrada en tu flota.')

        vehiculo = next(v for v in scope['vehiculos'] if str(v['_id']) == vehiculo_id)

        # Sin proyección: el detalle necesita puntosArreglar completo, y todo lo
        # que no sea público lo recorta `_sanitizar_os_detalle` antes de responder.
        ordenes = _ordenes_de_flotilla(db, scope, None, vehiculo_id=vehiculo_id)
        ordenes.sort(key=_fecha_doc, reverse=True)
        ordenes = ordenes[:MAX_OS_POR_UNIDAD]

        historial = [_sanitizar_os_detalle(o, scope['cliente_nombres']) for o in ordenes]
        realizados = [h for h in historial if h['estado'] not in ESTADOS_CANCELADOS]

        # Citas vigentes de la unidad: el flotillero quiere saber cuándo entra.
        # `citas` usa camelCase (vehiculoId), a diferencia de `ordenes_servicio`.
        citas = [
            {
                'fecha': c.get('fecha'),
                'hora': c.get('horaInicio'),
                'estado': c.get('estado'),
                'servicio': c.get('servicio'),
            }
            for c in db['citas'].find(
                {'vehiculoId': vehiculo_id, 'estado': {'$in': CITAS_VIGENTES}},
                {'fecha': 1, 'horaInicio': 1, 'estado': 1, 'servicio': 1},
            ).sort('fecha', 1)
        ]
        gasto_unidad = sum(h['total'] for h in realizados)

        return create_response(200, '360° de la unidad', {
            'vehiculo': {
                'id': vehiculo_id,
                'placas': vehiculo.get('placas') or '',
                'marca': vehiculo.get('marca'),
                'modelo': vehiculo.get('modelo'),
                'anio': vehiculo.get('anio'),
                'color': vehiculo.get('color'),
                'vin': vehiculo.get('vin'),
                'kilometraje': vehiculo.get('kilometraje') or 0,
                'titular': scope['cliente_nombres'].get(vehiculo.get('cliente_id'), ''),
                'proximo_cambio_aceite': vehiculo.get('proximo_cambio_aceite') or 0,
                'proximo_cambio_bujias': vehiculo.get('proximo_cambio_bujias') or 0,
                'proximo_cambio_aceite_fecha': vehiculo.get('proximo_cambio_aceite_fecha') or '',
                'proximo_cambio_bujias_fecha': vehiculo.get('proximo_cambio_bujias_fecha') or '',
            },
            'metricas': {
                'num_servicios': len(realizados),
                'gasto_total': round(gasto_unidad, 2),
                'ultimo_servicio': realizados[0]['fecha'] if realizados else None,
            },
            'citas': citas,
            'historial': historial,
        })
    except Exception as e:
        return handle_exception(e)
