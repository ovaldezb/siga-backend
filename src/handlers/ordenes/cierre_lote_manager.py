"""Cierre en bloque de órdenes en FINALIZADO.

Muchas órdenes se quedan en FINALIZADO aunque el cliente ya pagó: el taller cobra y
no lo registra. POST /ordenes/cerrar-lote las cierra de una vez con un mismo
método de pago, por el mismo camino que caja (nada se escribe por fuera):

  · Con venta (crédito con saldo): se registra un abono por el saldo completo con
    `registrar_abono_handler`; al liquidarse, la OS pasa a ENTREGADO.
  · Sin venta (nunca pasó por caja): se crea la venta con `create_venta_handler`,
    armando el carrito igual que el POS al importar la OS. Descuenta inventario,
    entra a caja y genera las CxP de piezas externas como cualquier venta.

Cada orden se procesa por separado y el resultado dice cuál se cerró y cuál no.
"""
import json
from aws_lambda_powertools import Logger
from bson import ObjectId
from bson.errors import InvalidId

from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.infrastructure.database import get_tenant_db
from src.shared.utils.auth_utils import get_claims, es_mecanico
from src.shared.utils.formas_pago_sat import resolver_forma_pago_sat, CODIGO_CREDITO, CATALOGO_FORMA_PAGO
from src.handlers.ventas.ventas_manager import create_venta_handler, registrar_abono_handler

logger = Logger()

# API Gateway corta a los 30 s y cada venta lleva su transacción: el front manda tandas.
MAX_ORDENES = 20


def _items_desde_orden(orden: dict) -> list[dict]:
    """Carrito de la OS, igual que `_cargarOrdenEnCarrito` del POS: las líneas con
    nombre y no rechazadas; la cortesía (`no_cobrar`) entra a $0."""
    items = []
    for punto in orden.get('puntosArreglar') or []:
        for linea in punto.get('items') or []:
            if not linea.get('nombre') or linea.get('rechazado'):
                continue
            cortesia = bool(linea.get('no_cobrar'))
            precio = float(linea.get('precioVenta') or linea.get('precio_venta') or 0)
            cantidad = int(linea.get('piezas') or 1)
            precio_compra = linea.get('precioCompra', linea.get('precio_compra')) or 0
            items.append({
                "linea_id": linea.get('linea_id'),
                "producto": {
                    "id": linea.get('item_id') or 'manual',
                    "nombre": linea.get('nombre'),
                    "no_parte": linea.get('noParte') or linea.get('no_parte') or '',
                    "precio_venta": precio,
                    "precio_compra": precio_compra,
                    "tipo": linea.get('tipo') or 'PRODUCTO',
                    "clave_sat": linea.get('clave_sat'),
                    "descripcion_clave_sat": linea.get('descripcion_clave_sat'),
                    "unidad_sat": linea.get('unidad_sat'),
                    "descripcion_unidad_sat": linea.get('descripcion_unidad_sat'),
                },
                "cantidad": cantidad,
                "tipo_precio": 'cortesia' if cortesia else 'publico',
                "precio_unitario": 0 if cortesia else precio,
                "subtotal": 0 if cortesia else round(cantidad * precio, 2),
                "es_externo": bool(linea.get('es_externo')),
                "proveedor_id": linea.get('proveedor_id'),
                "proveedor_nombre": linea.get('proveedor_nombre'),
                "costo_proveedor": linea.get('costo_proveedor', precio_compra) or 0,
                "incluye_iva": False,
            })
    return items


def _nombre_cliente(orden: dict) -> str:
    c = orden.get('cliente_snapshot') or {}
    nombre = " ".join(str(c.get(k) or '').strip() for k in ('nombre', 'apellido_paterno', 'apellido_materno'))
    return " ".join(nombre.split()) or 'Público General'


def _respuesta(resp: dict) -> tuple[bool, str, dict]:
    cuerpo = json.loads(resp.get('body') or '{}')
    return resp.get('statusCode', 500) < 300, cuerpo.get('message') or '', cuerpo.get('data') or {}


def _sub_evento(event: dict, body: dict, path: dict | None = None) -> dict:
    """Mismo usuario y claims que la petición original, con otro body."""
    sub = {k: v for k, v in event.items() if k not in ('body', 'pathParameters', 'queryStringParameters')}
    sub['body'] = json.dumps(body)
    if path:
        sub['pathParameters'] = path
    return sub


@logger.inject_lambda_context
def cerrar_ordenes_lote_handler(event, context):
    """POST /ordenes/cerrar-lote — body {orden_ids, metodo, forma_pago_sat?, referencia?,
    fecha_cierre?, sucursal_id?}. La venta se registra en la sucursal de cada OS; la
    `sucursal_id` del body sólo se usa para OS legacy que no la tienen."""
    try:
        claims = get_claims(event)
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No autorizado")
        if es_mecanico(claims):
            return create_response(403, "Tu usuario no tiene acceso a la informacion de cobros.")

        body = json.loads(event.get('body') or '{}')
        orden_ids = body.get('orden_ids')
        if not isinstance(orden_ids, list) or not orden_ids:
            return create_response(400, "Selecciona al menos una orden.")
        if len(orden_ids) > MAX_ORDENES:
            return create_response(400, f"Máximo {MAX_ORDENES} órdenes por lote.")
        metodo = str(body.get('metodo') or '').strip()
        if not metodo:
            return create_response(400, "Indica con qué método pagó el cliente.")
        referencia = str(body.get('referencia') or '').strip()
        fecha_cierre = body.get('fecha_cierre') or None
        sucursal_default = body.get('sucursal_id') or None

        db = get_tenant_db(tenant_id)
        cfg = db["configuracion"].find_one({"tenant_id": tenant_id}, {"metodos_pago": 1}) or {}
        forma_pago_sat = str(body.get('forma_pago_sat') or '').strip() or \
            resolver_forma_pago_sat(metodo, cfg.get('metodos_pago') or [])
        if not forma_pago_sat:
            return create_response(400, f"El método '{metodo}' no tiene código SAT; configúralo en Configuración.")
        if forma_pago_sat not in CATALOGO_FORMA_PAGO:
            return create_response(400, f"El código SAT '{forma_pago_sat}' no existe en el catálogo c_FormaPago.")
        if forma_pago_sat == CODIGO_CREDITO:
            return create_response(400, "El cierre en bloque es para órdenes ya pagadas: elige con qué pagó el cliente.")

        # Como en caja: la venta guarda el id del método (lo que manda el POS) y el
        # abono su nombre (lo que manda Cobranza).
        config_metodo = next((m for m in cfg.get('metodos_pago') or []
                              if isinstance(m, dict) and str(m.get('id') or '') == metodo), None)
        metodo_abono = str((config_metodo or {}).get('nombre') or metodo).strip().upper()

        resultados = []
        for raw_id in dict.fromkeys(str(i) for i in orden_ids):
            resultados.append(_cerrar_una(event, db, raw_id, metodo, metodo_abono, forma_pago_sat, referencia,
                                          fecha_cierre, sucursal_default))

        cerradas = sum(1 for r in resultados if r['ok'])
        return create_response(200, f"{cerradas} de {len(resultados)} órdenes cerradas", {
            "resultados": resultados,
            "cerradas": cerradas,
            "fallidas": len(resultados) - cerradas,
        })
    except Exception as e:
        return handle_exception(e, event)


def _cerrar_una(event, db, orden_id, metodo, metodo_abono, forma_pago_sat, referencia, fecha_cierre,
                sucursal_default) -> dict:
    resultado = {"orden_id": orden_id, "folio": None, "ok": False, "accion": None, "mensaje": ""}
    try:
        orden = db["ordenes_servicio"].find_one({"_id": ObjectId(orden_id)})
    except (InvalidId, TypeError):
        orden = None
    if not orden:
        resultado["mensaje"] = "Orden no encontrada."
        return resultado
    resultado["folio"] = orden.get('folio')
    if orden.get('estado') != 'FINALIZADO':
        resultado["mensaje"] = f"Está en {orden.get('estado') or 'sin estado'}, no en FINALIZADO."
        return resultado

    venta_id = orden.get('venta_id')
    if venta_id:
        try:
            venta = db["ventas"].find_one({"_id": ObjectId(venta_id)}, {"saldo_pendiente": 1, "folio": 1})
        except (InvalidId, TypeError):
            venta = None
        saldo = float((venta or {}).get('saldo_pendiente') or 0)
        if saldo <= 0:
            resultado["mensaje"] = "Su venta ya no tiene saldo; revísala en Órdenes."
            return resultado
        ok, mensaje, _ = _respuesta(registrar_abono_handler(_sub_evento(event, {
            "monto": saldo, "metodo": metodo_abono, "forma_pago_sat": forma_pago_sat, "referencia": referencia,
        }, {"id": str(venta_id)}), None))
        resultado.update(ok=ok, accion="LIQUIDADA" if ok else None, mensaje=mensaje,
                         venta_folio=(venta or {}).get('folio'), monto=round(saldo, 2))
        return resultado

    items = _items_desde_orden(orden)
    if not items:
        resultado["mensaje"] = "No tiene conceptos por cobrar."
        return resultado
    # Mano de obra en $0 casi siempre es un precio olvidado: el POS tampoco la deja pasar.
    sin_precio = next((i for i in items if i['producto']['tipo'] == 'SERVICIO'
                       and i['tipo_precio'] != 'cortesia' and not i['precio_unitario'] > 0), None)
    if sin_precio:
        resultado["mensaje"] = f"\"{sin_precio['producto']['nombre']}\" no tiene precio: cóbrala desde el POS."
        return resultado

    total = round(sum(i['precio_unitario'] * i['cantidad'] for i in items), 2)
    pagos = [{"metodo": metodo, "monto": total, "referencia": referencia, "forma_pago_sat": forma_pago_sat}] if total > 0 else []
    cliente = orden.get('cliente_snapshot') or {}
    ok, mensaje, data = _respuesta(create_venta_handler(_sub_evento(event, {
        "cliente_id": cliente.get('id') or orden.get('cliente_id') or 'PUBLICO_GENERAL',
        "cliente_nombre": _nombre_cliente(orden),
        "items": items,
        "metodo_pago": metodo,
        "pagos": pagos,
        "forma_pago_sat": forma_pago_sat,
        "descuento": 0,
        "subtotal": total,
        "iva": 0,
        "total": total,
        "orden_id": orden_id,
        "sucursal_id": orden.get('sucursal_id') or sucursal_default,
        "fecha_cierre": fecha_cierre,
        "venta_facturada": False,
    }), None))
    resultado.update(ok=ok, accion="COBRADA" if ok else None, mensaje=mensaje,
                     venta_folio=data.get('folio'), monto=total)
    return resultado
