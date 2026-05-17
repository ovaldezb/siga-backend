"""Enlace público para que el cliente apruebe/rechace items de su cotización.

Flujo:
1. Asesor genera un enlace (POST /ordenes/{id}/cliente-link).
   - Crea/rota un `nonce` y un challenge derivado de placa+teléfono.
   - Devuelve URL + token firmado HMAC + texto del challenge (para que el asesor
     pueda guiar al cliente por teléfono si se confunde).
2. Cliente abre la URL, el SPA llama:
   - GET /public/cotizacion/challenge?token=...      → prompt + slots a llenar.
   - POST /public/cotizacion/verify {token, answer}  → si OK, devuelve session_token.
   - GET /public/cotizacion?session_token=...        → cotización filtrada (sin cortesías).
   - POST /public/cotizacion/decidir                 → graba decisiones con source=client_link.

Seguridad:
- Token = HMAC-SHA256 con secreto en env (CLIENT_LINK_SECRET).
- Nonce persistido en collection `cotizacion_acceso`; rotar = invalidar links viejos.
- 5 intentos por nonce, después se bloquea hasta que el asesor regenere.
- Items con `no_cobrar=true` se filtran ANTES de salir del backend.
- Cliente nunca puede tocar items en estado APROBADO/EN_PROCESO/FINALIZADO.
"""

import base64
import hashlib
import hmac
import json
import os
import random
import re
import secrets
from datetime import datetime, timedelta
from typing import Optional, Tuple

from aws_lambda_powertools import Logger
from bson import ObjectId

from src.shared.infrastructure.database import get_tenant_db
from src.shared.utils.auth_utils import get_claims
from src.shared.utils.date_utils import iso_utc
from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.utils.indexes import ensure_indexes

logger = Logger()

TOKEN_TTL_DAYS = 14
SESSION_TTL_MIN = 30
MAX_VERIFY_ATTEMPTS = 5

# Recetas (placa_take, phone_take); cada take = (mode, count). 'mode' ∈ {first, last}
CHALLENGE_RECIPES = [
    (('last', 2), ('first', 3)),
    (('first', 2), ('last', 4)),
    (('last', 3), ('last', 2)),
    (('first', 3), ('first', 3)),
    (('last', 2), ('last', 4)),
    (('first', 4), ('last', 3)),
]


# ---------- helpers de firma ----------

def _get_secret() -> bytes:
    s = os.environ.get('CLIENT_LINK_SECRET', '')
    if not s:
        # Fallback dev-only para no romper local sin SSM. Logueamos warning para detectarlo.
        s = 'dev-fallback-' + os.environ.get('MONGO_HOST', 'localhost')
        logger.warning('CLIENT_LINK_SECRET no configurado; usando fallback de desarrollo')
    return s.encode('utf-8')


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b'=').decode('ascii')


def _b64u_dec(s: str) -> bytes:
    pad = '=' * (-len(s) % 4)
    return base64.urlsafe_b64decode((s + pad).encode('ascii'))


def _sign(payload: dict) -> str:
    body = json.dumps(payload, separators=(',', ':'), sort_keys=True).encode('utf-8')
    sig = hmac.new(_get_secret(), body, hashlib.sha256).digest()
    return f"{_b64u(body)}.{_b64u(sig)}"


def _verify(token: str) -> Optional[dict]:
    try:
        body_b64, sig_b64 = token.split('.', 1)
        body = _b64u_dec(body_b64)
        sig = _b64u_dec(sig_b64)
        expected = hmac.new(_get_secret(), body, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(body.decode('utf-8'))
        exp = payload.get('exp')
        if exp and datetime.utcnow().timestamp() > exp:
            return None
        return payload
    except Exception:
        return None


def _hash_answer(answer: str) -> str:
    return hmac.new(_get_secret(), answer.encode('utf-8'), hashlib.sha256).hexdigest()


# ---------- challenge ----------

def _digits(s: str) -> str:
    return re.sub(r'\D', '', s or '')


def _take(digits: str, mode: str, count: int) -> str:
    if not digits or count <= 0:
        return ''
    if len(digits) < count:
        return digits
    return digits[:count] if mode == 'first' else digits[-count:]


def _label(slot: dict) -> str:
    word = 'placa' if slot['src'] == 'placa' else 'teléfono'
    adj = 'primeros' if slot['mode'] == 'first' else 'últimos'
    return f'{adj.capitalize()} {slot["count"]} dígitos de tu {word}'


def _build_challenge(placa: str, telefono: str) -> Optional[dict]:
    """Devuelve {spec, prompt, slots, expected_hash, expected_plain, degraded} o None."""
    p = _digits(placa)
    t = _digits(telefono)
    have_p = len(p) >= 2
    have_t = len(t) >= 3

    if not have_p and not have_t:
        return None

    # Modo degradado: usa lo que haya
    if not have_p or not have_t:
        src = 'phone' if have_t else 'placa'
        digits = t if have_t else p
        count = 4 if len(digits) >= 4 else len(digits)
        spec = [{'src': src, 'mode': 'last', 'count': count}]
        slots = [{'label': _label(spec[0]), 'length': count}]
        expected = _take(digits, 'last', count)
        return {
            'spec': spec,
            'prompt': _label(spec[0]),
            'slots': slots,
            'expected_hash': _hash_answer(expected),
            'expected_plain': expected,
            'degraded': True,
        }

    # Modo completo: elige una receta cubrible
    viables = [(pr, tr) for pr, tr in CHALLENGE_RECIPES if len(p) >= pr[1] and len(t) >= tr[1]]
    if not viables:
        viables = [(('last', min(2, len(p))), ('last', min(3, len(t))))]
    placa_rule, phone_rule = random.choice(viables)

    placa_slot = {'src': 'placa', 'mode': placa_rule[0], 'count': placa_rule[1]}
    phone_slot = {'src': 'phone', 'mode': phone_rule[0], 'count': phone_rule[1]}
    spec = [placa_slot, phone_slot] if random.choice([True, False]) else [phone_slot, placa_slot]

    slots = [{'label': _label(s), 'length': s['count']} for s in spec]
    prompt = ' + '.join(_label(s) for s in spec)

    parts = []
    for s in spec:
        digits = p if s['src'] == 'placa' else t
        parts.append(_take(digits, s['mode'], s['count']))
    expected = ''.join(parts)

    return {
        'spec': spec,
        'prompt': prompt,
        'slots': slots,
        'expected_hash': _hash_answer(expected),
        'expected_plain': expected,
        'degraded': False,
    }


def _recompute_expected(spec, placa, telefono) -> str:
    p = _digits(placa)
    t = _digits(telefono)
    parts = []
    for s in spec or []:
        d = p if s.get('src') == 'placa' else t
        parts.append(_take(d, s.get('mode', 'last'), int(s.get('count', 0))))
    return ''.join(parts)


# ---------- OS lookup ----------

def _get_orden_y_datos(db, orden_id: str) -> Tuple[Optional[dict], str, str]:
    try:
        orden = db['ordenes_servicio'].find_one({'_id': ObjectId(orden_id)})
    except Exception:
        return None, '', ''
    if not orden:
        return None, '', ''
    cliente = orden.get('cliente_snapshot') or {}
    telefono = cliente.get('telefono') or ''
    placa = ''
    v_id = orden.get('vehiculo_id')
    if v_id and isinstance(v_id, str) and len(v_id) == 24:
        try:
            v = db['vehiculos'].find_one({'_id': ObjectId(v_id)}, {'placas': 1})
            if v:
                placa = v.get('placas') or ''
        except Exception:
            pass
    if not placa:
        placa = (orden.get('vehiculo_snapshot') or {}).get('placas', '')
    return orden, placa, telefono


def _build_link_url(token: str) -> str:
    base = os.environ.get('CLIENT_LINK_BASE_URL', '').rstrip('/')
    if not base:
        # Default razonable; el asesor copia el link y se lo manda al cliente, así que
        # el front debe registrar la ruta /cliente/cotizacion/:token
        base = 'https://app.example.com/#/cliente/cotizacion'
    return f"{base}/{token}"


# ---------- handlers internos (con Cognito) ----------

@logger.inject_lambda_context
def create_cliente_link_handler(event, context):
    """POST /ordenes/{id}/cliente-link — Genera o rota el enlace público."""
    try:
        claims = get_claims(event)
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, 'No se encontró un tenantId asociado.')

        orden_id = event['pathParameters']['id']
        db = get_tenant_db(tenant_id)
        ensure_indexes(db, tenant_id)

        orden, placa, telefono = _get_orden_y_datos(db, orden_id)
        if not orden:
            return create_response(404, 'Orden no encontrada.')

        ch = _build_challenge(placa, telefono)
        if not ch:
            return create_response(
                400,
                'El cliente no tiene placa ni teléfono registrados; no se puede generar enlace seguro.'
            )

        nonce = secrets.token_hex(16)
        exp_dt = datetime.utcnow() + timedelta(days=TOKEN_TTL_DAYS)
        responsable = claims.get('email') or 'system'

        db['cotizacion_acceso'].update_one(
            {'orden_id': orden_id},
            {'$set': {
                'orden_id': orden_id,
                'nonce': nonce,
                'created_at': iso_utc(),
                'created_by': responsable,
                'expires_at': iso_utc(exp_dt),
                'intentos_verificacion': 0,
                'bloqueado_en': None,
                'ultimo_acceso': None,
                'challenge': {
                    'spec': ch['spec'],
                    'prompt': ch['prompt'],
                    'slots': ch['slots'],
                    'expected_hash': ch['expected_hash'],
                    'degraded': ch['degraded'],
                },
            }},
            upsert=True,
        )

        token = _sign({
            't': tenant_id,
            'o': orden_id,
            'n': nonce,
            'exp': int(exp_dt.timestamp()),
        })

        return create_response(200, 'Link generado', {
            'url': _build_link_url(token),
            'token': token,
            'expires_at': iso_utc(exp_dt),
            'challenge_prompt': ch['prompt'],
            'challenge_expected': ch['expected_plain'],  # solo asesor lo ve
            'degraded': ch['degraded'],
        })
    except Exception as e:
        return handle_exception(e)


@logger.inject_lambda_context
def get_cliente_link_handler(event, context):
    """GET /ordenes/{id}/cliente-link — Devuelve estado del link vigente sin rotar."""
    try:
        claims = get_claims(event)
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, 'No se encontró un tenantId asociado.')

        orden_id = event['pathParameters']['id']
        db = get_tenant_db(tenant_id)

        acceso = db['cotizacion_acceso'].find_one({'orden_id': orden_id})
        if not acceso:
            return create_response(404, 'No hay enlace vigente. Genera uno nuevo.')

        _, placa, telefono = _get_orden_y_datos(db, orden_id)
        spec = (acceso.get('challenge') or {}).get('spec') or []
        expected_plain = _recompute_expected(spec, placa, telefono)

        try:
            exp_dt = datetime.fromisoformat((acceso.get('expires_at') or '').rstrip('Z'))
        except Exception:
            exp_dt = datetime.utcnow() + timedelta(days=TOKEN_TTL_DAYS)

        token = _sign({
            't': tenant_id,
            'o': orden_id,
            'n': acceso['nonce'],
            'exp': int(exp_dt.timestamp()),
        })

        return create_response(200, 'Link recuperado', {
            'url': _build_link_url(token),
            'token': token,
            'expires_at': acceso.get('expires_at'),
            'created_at': acceso.get('created_at'),
            'created_by': acceso.get('created_by'),
            'intentos_verificacion': acceso.get('intentos_verificacion', 0),
            'bloqueado_en': acceso.get('bloqueado_en'),
            'ultimo_acceso': acceso.get('ultimo_acceso'),
            'challenge_prompt': (acceso.get('challenge') or {}).get('prompt'),
            'challenge_expected': expected_plain,
            'degraded': (acceso.get('challenge') or {}).get('degraded', False),
        })
    except Exception as e:
        return handle_exception(e)


@logger.inject_lambda_context
def revoke_cliente_link_handler(event, context):
    """DELETE /ordenes/{id}/cliente-link — Revoca el acceso público."""
    try:
        claims = get_claims(event)
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, 'No se encontró un tenantId asociado.')

        orden_id = event['pathParameters']['id']
        db = get_tenant_db(tenant_id)
        result = db['cotizacion_acceso'].delete_one({'orden_id': orden_id})
        if result.deleted_count == 0:
            return create_response(404, 'No había link vigente.')
        return create_response(200, 'Link revocado')
    except Exception as e:
        return handle_exception(e)


# ---------- helpers públicos ----------

def _decode_token(token: str) -> Tuple[Optional[str], Optional[dict], Optional[str]]:
    if not token:
        return None, None, 'Falta token'
    payload = _verify(token)
    if not payload:
        return None, None, 'Token inválido o expirado'
    return payload.get('t'), payload, None


def _client_ip(event) -> str:
    return ((event.get('requestContext') or {}).get('identity') or {}).get('sourceIp', '')


def _user_agent(event) -> str:
    h = event.get('headers') or {}
    return h.get('User-Agent') or h.get('user-agent') or ''


# Campos visibles al cliente. TODO si agregamos descripcion en items, agregarlo aquí.
_PUBLIC_ITEM_FIELDS = {
    'item_id', 'nombre', 'descripcion', 'noParte', 'marca',
    'piezas', 'precioVenta', 'subtotal',
    'aprobado', 'rechazado', 'decision',
    'tipo',
}


def _sanitize_orden(orden: dict) -> dict:
    puntos_pub = []
    for p_idx, punto in enumerate(orden.get('puntosArreglar') or []):
        items_pub = []
        for i_idx, item in enumerate(punto.get('items') or []):
            if item.get('no_cobrar'):
                continue
            filt = {k: v for k, v in item.items() if k in _PUBLIC_ITEM_FIELDS}
            filt['_punto_idx'] = p_idx
            filt['_item_idx'] = i_idx
            if 'decision' not in filt:
                if item.get('rechazado'):
                    filt['decision'] = 'rechazado'
                elif item.get('aprobado') is True:
                    # Servidor decidió que el cliente debe decidir; "pendiente" en su vista.
                    filt['decision'] = 'pendiente'
                else:
                    filt['decision'] = 'pendiente'
            items_pub.append(filt)
        if items_pub:
            puntos_pub.append({'nombre': punto.get('nombre'), 'items': items_pub})

    cliente = orden.get('cliente_snapshot') or {}
    vehiculo = orden.get('vehiculo_snapshot') or {}
    nombre_cliente = ' '.join(filter(None, [cliente.get('nombre'), cliente.get('apellido_paterno')])).strip()
    return {
        'folio': orden.get('folio'),
        'estado': orden.get('estado'),
        'cliente_nombre': nombre_cliente or 'Cliente',
        'vehiculo': {
            'marca': vehiculo.get('marca'),
            'modelo': vehiculo.get('modelo'),
            'anio': vehiculo.get('anio'),
            'placas': vehiculo.get('placas'),
            'color': vehiculo.get('color'),
        },
        'puntosArreglar': puntos_pub,
        'falla_reportada': orden.get('falla_reportada'),
        'diagnostico': orden.get('diagnostico'),
        'fecha': orden.get('createdAt').isoformat() if isinstance(orden.get('createdAt'), datetime) else orden.get('createdAt'),
        'fechaEstimadaEntrega': orden.get('fechaEstimadaEntrega'),
    }


# ---------- handlers públicos (SIN authorizer) ----------

@logger.inject_lambda_context
def public_get_challenge_handler(event, context):
    """GET /public/cotizacion/challenge?token=... — Prompt + slots para el cliente."""
    try:
        qp = event.get('queryStringParameters') or {}
        tenant_id, payload, err = _decode_token(qp.get('token', ''))
        if err:
            return create_response(401, err)

        db = get_tenant_db(tenant_id)
        orden_id = payload['o']
        nonce = payload['n']

        acceso = db['cotizacion_acceso'].find_one({'orden_id': orden_id})
        if not acceso or acceso.get('nonce') != nonce:
            return create_response(401, 'Enlace ya no es válido (fue regenerado o revocado).')
        if acceso.get('bloqueado_en'):
            return create_response(403, 'Demasiados intentos. Pide a tu taller un nuevo enlace.')

        orden = db['ordenes_servicio'].find_one(
            {'_id': ObjectId(orden_id)},
            {'folio': 1, 'cliente_snapshot.nombre': 1}
        )
        if not orden:
            return create_response(404, 'Cotización no encontrada.')

        ch = acceso.get('challenge') or {}
        return create_response(200, 'Challenge', {
            'folio': orden.get('folio'),
            'cliente_nombre': (orden.get('cliente_snapshot') or {}).get('nombre'),
            'prompt': ch.get('prompt'),
            'slots': ch.get('slots'),
            'intentos_restantes': max(0, MAX_VERIFY_ATTEMPTS - acceso.get('intentos_verificacion', 0)),
        })
    except Exception as e:
        return handle_exception(e)


@logger.inject_lambda_context
def public_verify_handler(event, context):
    """POST /public/cotizacion/verify  Body: {token, answer}

    `answer` puede ser string concatenado o array (un valor por slot, en el orden del prompt).
    """
    try:
        body = json.loads(event.get('body') or '{}')
        token = body.get('token', '')
        raw = body.get('answer', '')
        answer = ''.join(str(x) for x in raw) if isinstance(raw, list) else str(raw)
        answer = _digits(answer)

        tenant_id, payload, err = _decode_token(token)
        if err:
            return create_response(401, err)

        db = get_tenant_db(tenant_id)
        orden_id = payload['o']
        nonce = payload['n']

        acceso = db['cotizacion_acceso'].find_one({'orden_id': orden_id})
        if not acceso or acceso.get('nonce') != nonce:
            return create_response(401, 'Enlace ya no es válido (fue regenerado o revocado).')
        if acceso.get('bloqueado_en'):
            return create_response(403, 'Demasiados intentos. Pide a tu taller un nuevo enlace.')

        expected_hash = (acceso.get('challenge') or {}).get('expected_hash', '')
        if not expected_hash or _hash_answer(answer) != expected_hash:
            intentos = acceso.get('intentos_verificacion', 0) + 1
            update = {'intentos_verificacion': intentos}
            if intentos >= MAX_VERIFY_ATTEMPTS:
                update['bloqueado_en'] = iso_utc()
            db['cotizacion_acceso'].update_one({'orden_id': orden_id}, {'$set': update})
            restantes = max(0, MAX_VERIFY_ATTEMPTS - intentos)
            return create_response(401, f'Respuesta incorrecta. Te quedan {restantes} intentos.',
                                   {'intentos_restantes': restantes})

        session_exp = datetime.utcnow() + timedelta(minutes=SESSION_TTL_MIN)
        session_token = _sign({
            't': tenant_id,
            'o': orden_id,
            'n': nonce,
            's': 1,
            'exp': int(session_exp.timestamp()),
        })

        db['cotizacion_acceso'].update_one(
            {'orden_id': orden_id},
            {'$set': {'ultimo_acceso': iso_utc(), 'intentos_verificacion': 0}}
        )

        return create_response(200, 'Acceso concedido', {
            'session_token': session_token,
            'expires_at': iso_utc(session_exp),
        })
    except Exception as e:
        return handle_exception(e)


@logger.inject_lambda_context
def public_get_cotizacion_handler(event, context):
    """GET /public/cotizacion?session_token=... — Cotización filtrada para el cliente."""
    try:
        qp = event.get('queryStringParameters') or {}
        tenant_id, payload, err = _decode_token(qp.get('session_token', ''))
        if err:
            return create_response(401, err)
        if payload.get('s') != 1:
            return create_response(401, 'Se requiere session_token (no token de acceso).')

        db = get_tenant_db(tenant_id)
        orden_id = payload['o']
        nonce = payload['n']

        acceso = db['cotizacion_acceso'].find_one({'orden_id': orden_id})
        if not acceso or acceso.get('nonce') != nonce:
            return create_response(401, 'Sesión inválida.')

        orden = db['ordenes_servicio'].find_one({'_id': ObjectId(orden_id)})
        if not orden:
            return create_response(404, 'Cotización no encontrada.')

        publica = _sanitize_orden(orden)
        publica['editable'] = orden.get('estado') in ('RECEPCION', 'COTIZADO')
        return create_response(200, 'Cotización', publica)
    except Exception as e:
        return handle_exception(e)


@logger.inject_lambda_context
def public_decidir_handler(event, context):
    """POST /public/cotizacion/decidir
       Body: {session_token, decisiones: [{punto_idx, item_idx, decision}]}
    """
    try:
        body = json.loads(event.get('body') or '{}')
        token = body.get('session_token', '')
        decisiones = body.get('decisiones') or []
        if not isinstance(decisiones, list) or not decisiones:
            return create_response(400, 'Decisiones vacías.')

        tenant_id, payload, err = _decode_token(token)
        if err:
            return create_response(401, err)
        if payload.get('s') != 1:
            return create_response(401, 'Se requiere session_token.')

        db = get_tenant_db(tenant_id)
        orden_id = payload['o']
        nonce = payload['n']

        acceso = db['cotizacion_acceso'].find_one({'orden_id': orden_id})
        if not acceso or acceso.get('nonce') != nonce:
            return create_response(401, 'Sesión inválida.')

        orden = db['ordenes_servicio'].find_one({'_id': ObjectId(orden_id)})
        if not orden:
            return create_response(404, 'Cotización no encontrada.')
        if orden.get('estado') not in ('RECEPCION', 'COTIZADO'):
            return create_response(409, 'La cotización ya no se puede modificar (orden en proceso o cerrada).')

        ip = _client_ip(event)
        ua = _user_agent(event)
        now = iso_utc()

        puntos = orden.get('puntosArreglar') or []
        modificados = 0
        for d in decisiones:
            try:
                p_idx = int(d.get('punto_idx'))
                i_idx = int(d.get('item_idx'))
                dec = d.get('decision')
            except Exception:
                continue
            if dec not in ('aprobado', 'rechazado'):
                continue
            if p_idx < 0 or p_idx >= len(puntos):
                continue
            items = puntos[p_idx].get('items') or []
            if i_idx < 0 or i_idx >= len(items):
                continue
            item = items[i_idx]
            if item.get('no_cobrar'):
                # El cliente nunca debió ver esto; lo ignoramos por defensa en profundidad.
                continue
            item['decision'] = dec
            item['decision_source'] = 'client_link'
            item['decided_by'] = None
            item['decided_at'] = now
            item['decided_meta'] = {'ip': ip, 'user_agent': ua}
            item['aprobado'] = (dec == 'aprobado')
            item['rechazado'] = (dec == 'rechazado')
            modificados += 1

        if modificados == 0:
            return create_response(400, 'Ninguna decisión válida en la petición.')

        from src.handlers.ordenes.ordenes_manager import _calcular_total_orden
        nuevo_total = _calcular_total_orden(puntos)

        db['ordenes_servicio'].update_one(
            {'_id': ObjectId(orden_id)},
            {'$set': {
                'puntosArreglar': puntos,
                'total': nuevo_total,
                'updatedAt': datetime.utcnow(),
            }}
        )
        db['cotizacion_acceso'].update_one({'orden_id': orden_id}, {'$set': {'ultimo_acceso': now}})

        return create_response(200, 'Decisiones registradas', {
            'modificados': modificados,
            'total': nuevo_total,
        })
    except Exception as e:
        return handle_exception(e)
