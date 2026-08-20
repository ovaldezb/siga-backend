"""Una pieza sale del almacén UNA sola vez.

Reporte del taller: las refacciones se descontaban dos veces — una al marcarlas
"entregado" en la orden de servicio y otra al cobrar esa misma orden en el punto
de venta. Estos tests fijan la regla: la salida física manda (la OS descuenta al
guardar) y el cobro sólo descuenta lo que la orden no haya surtido todavía.
"""
import json

from bson import ObjectId

from src.handlers.ordenes.ordenes_manager import update_orden_handler
from src.handlers.ventas.ventas_manager import create_venta_handler

TENANT = "tallertest"
SUCURSAL = "suc-a"


def _claims():
    return {"custom:tenant_id": TENANT, "sub": "u-1", "email": "asesor@taller.com", "name": "Asesor"}


def _seed_item(db, *, stock=10, nombre="Balata delantera"):
    return str(db["items"].insert_one({
        "nombre": nombre,
        "no_parte": "NP-BAL",
        "sucursal_id": SUCURSAL,
        "stock": stock,
        "precio_compra": 300.0,
        "costo_promedio": 300.0,
        "precio_venta": 800.0,
        "maneja_inventario": True,
        "tipo": "REFACCION",
        "tenant_id": TENANT,
    }).inserted_id)


def _seed_orden(db, item_id, *, piezas=2, entregado=False, linea_id="linea-1"):
    return str(db["ordenes_servicio"].insert_one({
        "tenant_id": TENANT,
        "sucursal_id": SUCURSAL,
        "folio": "OS-1",
        "estado": "EN_PROCESO",
        "vehiculo_id": str(ObjectId()),
        "cliente_snapshot": {"id": "cli-1", "nombre": "Juan"},
        "puntosArreglar": [{"nombre": "Frenos", "items": [{
            "linea_id": linea_id,
            "item_id": item_id,
            "nombre": "Balata delantera",
            "noParte": "NP-BAL",
            "piezas": piezas,
            "precioVenta": 800.0,
            "precioCompra": 300.0,
            "subtotal": 800.0 * piezas,
            "aprobado": True,
            "entregado": entregado,
            "tipo": "PRODUCTO",
        }]}],
    }).inserted_id)


def _linea(db, orden_id):
    doc = db["ordenes_servicio"].find_one({"_id": ObjectId(orden_id)})
    return doc["puntosArreglar"][0]["items"][0]


def _stock(db, item_id):
    return db["items"].find_one({"_id": ObjectId(item_id)})["stock"]


def _update_event(orden_id, puntos=None, **campos):
    body = dict(campos)
    if puntos is not None:
        body["puntosArreglar"] = puntos
    return {
        "pathParameters": {"id": orden_id},
        "body": json.dumps(body),
        "requestContext": {"authorizer": {"claims": _claims()}},
    }


def _puntos_con(item_id, *, piezas=2, entregado=True, linea_id="linea-1", **extra):
    item = {
        "linea_id": linea_id,
        "item_id": item_id,
        "nombre": "Balata delantera",
        "noParte": "NP-BAL",
        "piezas": piezas,
        "precioVenta": 800.0,
        "precioCompra": 300.0,
        "subtotal": 800.0 * piezas,
        "aprobado": True,
        "entregado": entregado,
        "tipo": "PRODUCTO",
    }
    item.update(extra)
    return [{"nombre": "Frenos", "items": [item]}]


def _venta_event(item_id, orden_id, *, cantidad=2, linea_id="linea-1"):
    producto = {"id": item_id, "nombre": "Balata delantera", "tipo": "REFACCION"}
    linea = {"producto": producto, "cantidad": cantidad, "precio_unitario": 800.0}
    if linea_id:
        linea["linea_id"] = linea_id
    body = {
        "sucursal_id": SUCURSAL,
        "cliente_id": "PUBLICO_GENERAL",
        "items": [linea],
        "metodo_pago": "EFECTIVO",
        "pagos": [{"metodo": "EFECTIVO", "monto": 800.0 * cantidad, "forma_pago_sat": "01"}],
        "orden_id": orden_id,
    }
    return {"body": json.dumps(body), "requestContext": {"authorizer": {"claims": _claims()}}}


# --- El bug reportado -------------------------------------------------------

def test_pieza_entregada_en_os_y_cobrada_en_pos_descuenta_una_sola_vez(mock_db):
    """El reporte original: marcar entregado + cobrar la OS bajaba el stock doble."""
    db = mock_db[f"t_{TENANT}"]
    item_id = _seed_item(db, stock=10)
    orden_id = _seed_orden(db, item_id, piezas=2)

    # 1. El mecánico surte la pieza y el asesor guarda la orden.
    resp = update_orden_handler(_update_event(orden_id, _puntos_con(item_id)), None)
    assert resp["statusCode"] == 200, resp["body"]
    assert _stock(db, item_id) == 8

    # 2. El cliente paga la orden en el punto de venta.
    resp = create_venta_handler(_venta_event(item_id, orden_id), None)
    assert resp["statusCode"] == 201, resp["body"]

    # La pieza ya había salido del almacén: el cobro no la descuenta otra vez.
    assert _stock(db, item_id) == 8


def test_pieza_no_entregada_se_descuenta_al_cobrar(mock_db):
    """Refacción que nunca se marcó "entregado": el POS sí debe descontarla."""
    db = mock_db[f"t_{TENANT}"]
    item_id = _seed_item(db, stock=10)
    orden_id = _seed_orden(db, item_id, piezas=2, entregado=False)

    resp = create_venta_handler(_venta_event(item_id, orden_id), None)
    assert resp["statusCode"] == 201, resp["body"]
    assert _stock(db, item_id) == 8


def test_cobro_estampa_el_consumo_en_la_os(mock_db):
    """Tras cobrar, marcar la pieza como entregada no puede volver a descontarla."""
    db = mock_db[f"t_{TENANT}"]
    item_id = _seed_item(db, stock=10)
    orden_id = _seed_orden(db, item_id, piezas=2, entregado=False)

    create_venta_handler(_venta_event(item_id, orden_id), None)
    assert _stock(db, item_id) == 8
    assert _linea(db, orden_id)["inventario_descontado_por"] == "VENTA"

    # El asesor reabre la OS y prende el toggle de entregado.
    resp = update_orden_handler(_update_event(orden_id, _puntos_con(item_id)), None)
    assert resp["statusCode"] == 200, resp["body"]
    assert _stock(db, item_id) == 8


def test_desmarcar_entregado_no_devuelve_piezas_ya_cobradas(mock_db):
    """La pieza vendida no regresa al anaquel por apagar un toggle."""
    db = mock_db[f"t_{TENANT}"]
    item_id = _seed_item(db, stock=10)
    orden_id = _seed_orden(db, item_id, piezas=2, entregado=False)

    create_venta_handler(_venta_event(item_id, orden_id), None)
    update_orden_handler(_update_event(orden_id, _puntos_con(item_id, entregado=False)), None)

    assert _stock(db, item_id) == 8


# --- Idempotencia y correcciones -------------------------------------------

def test_editar_una_os_ya_cobrada_no_devuelve_inventario(mock_db):
    """La OS tiene venta: ninguna edición posterior repone piezas vendidas."""
    db = mock_db[f"t_{TENANT}"]
    item_id = _seed_item(db, stock=10)
    orden_id = _seed_orden(db, item_id, piezas=2)

    update_orden_handler(_update_event(orden_id, _puntos_con(item_id)), None)
    create_venta_handler(_venta_event(item_id, orden_id), None)
    assert _stock(db, item_id) == 8

    # Un admin borra la línea de una orden ya cobrada.
    resp = update_orden_handler(_update_event(orden_id, [{"nombre": "Frenos", "items": []}]), None)
    assert resp["statusCode"] == 200, resp["body"]
    assert _stock(db, item_id) == 8


def test_reguardar_la_misma_orden_no_mueve_inventario(mock_db):
    """Guardar dos veces sin cambios deja el stock igual."""
    db = mock_db[f"t_{TENANT}"]
    item_id = _seed_item(db, stock=10)
    orden_id = _seed_orden(db, item_id, piezas=2)

    update_orden_handler(_update_event(orden_id, _puntos_con(item_id)), None)
    assert _stock(db, item_id) == 8

    puntos = [{"nombre": "Frenos", "items": [_linea(db, orden_id)]}]
    update_orden_handler(_update_event(orden_id, puntos), None)
    assert _stock(db, item_id) == 8


def test_desmarcar_entregado_devuelve_la_pieza(mock_db):
    """Se marcó por error: apagar el toggle regresa el stock al guardar."""
    db = mock_db[f"t_{TENANT}"]
    item_id = _seed_item(db, stock=10)
    orden_id = _seed_orden(db, item_id, piezas=2)

    update_orden_handler(_update_event(orden_id, _puntos_con(item_id)), None)
    assert _stock(db, item_id) == 8

    update_orden_handler(_update_event(orden_id, _puntos_con(item_id, entregado=False)), None)
    assert _stock(db, item_id) == 10
    assert _linea(db, orden_id)["inventario_descontado_piezas"] == 0


def test_subir_piezas_descuenta_solo_el_faltante(mock_db):
    """De 2 a 3 piezas surtidas sale 1 más del almacén, no 3."""
    db = mock_db[f"t_{TENANT}"]
    item_id = _seed_item(db, stock=10)
    orden_id = _seed_orden(db, item_id, piezas=2)

    update_orden_handler(_update_event(orden_id, _puntos_con(item_id, piezas=2)), None)
    assert _stock(db, item_id) == 8

    update_orden_handler(_update_event(orden_id, _puntos_con(item_id, piezas=3)), None)
    assert _stock(db, item_id) == 7


def test_borrar_la_linea_devuelve_lo_surtido(mock_db):
    """Quitar de la orden una pieza ya surtida la regresa al inventario."""
    db = mock_db[f"t_{TENANT}"]
    item_id = _seed_item(db, stock=10)
    orden_id = _seed_orden(db, item_id, piezas=2)

    update_orden_handler(_update_event(orden_id, _puntos_con(item_id)), None)
    assert _stock(db, item_id) == 8

    update_orden_handler(_update_event(orden_id, [{"nombre": "Frenos", "items": []}]), None)
    assert _stock(db, item_id) == 10


def test_cancelar_la_orden_devuelve_lo_surtido(mock_db):
    """Cancelar desde el listado (sin mandar items) repone el almacén."""
    db = mock_db[f"t_{TENANT}"]
    item_id = _seed_item(db, stock=10)
    orden_id = _seed_orden(db, item_id, piezas=2)

    update_orden_handler(_update_event(orden_id, _puntos_con(item_id)), None)
    assert _stock(db, item_id) == 8

    resp = update_orden_handler(
        _update_event(orden_id, estado="CANCELADO", motivo_cancelacion="Cliente desistió"), None)
    assert resp["statusCode"] == 200, resp["body"]
    assert _stock(db, item_id) == 10


def test_item_rechazado_no_descuenta(mock_db):
    """Una sugerencia rechazada por el cliente no toca inventario."""
    db = mock_db[f"t_{TENANT}"]
    item_id = _seed_item(db, stock=10)
    orden_id = _seed_orden(db, item_id, piezas=2)

    update_orden_handler(
        _update_event(orden_id, _puntos_con(item_id, rechazado=True, aprobado=False)), None)
    assert _stock(db, item_id) == 10


def test_stock_insuficiente_rechaza_el_guardado(mock_db):
    """No se puede surtir lo que no hay: 409 y la orden no queda guardada."""
    db = mock_db[f"t_{TENANT}"]
    item_id = _seed_item(db, stock=1)
    orden_id = _seed_orden(db, item_id, piezas=2)

    resp = update_orden_handler(_update_event(orden_id, _puntos_con(item_id, piezas=2)), None)
    assert resp["statusCode"] == 409
    assert "Stock insuficiente" in json.loads(resp["body"])["message"]
    assert _stock(db, item_id) == 1


def test_bitacora_registra_la_salida_por_orden(mock_db):
    """El kardex debe explicar la salida: concepto CONSUMO_OS con folio de la OS."""
    db = mock_db[f"t_{TENANT}"]
    item_id = _seed_item(db, stock=10)
    orden_id = _seed_orden(db, item_id, piezas=2)

    update_orden_handler(_update_event(orden_id, _puntos_con(item_id)), None)

    movs = list(db["inventario_movimientos"].find({"item_id": item_id}))
    assert len(movs) == 1
    assert movs[0]["concepto"] == "CONSUMO_OS"
    assert movs[0]["cantidad"] == -2
    assert movs[0]["stock_resultante"] == 8
    assert movs[0]["referencia_folio"] == "OS-1"


def test_os_historica_entregada_no_se_vuelve_a_descontar(mock_db):
    """Líneas anteriores al fix: el toggle viejo ya bajó su stock al vuelo.

    Guardar hoy una de esas órdenes no puede repetir el descuento, aunque la línea
    no tenga ninguno de los campos nuevos de consumo.
    """
    db = mock_db[f"t_{TENANT}"]
    item_id = _seed_item(db, stock=8)  # ya se le había restado 2 en su momento
    orden_id = _seed_orden(db, item_id, piezas=2, entregado=True, linea_id=None)

    puntos = _puntos_con(item_id, linea_id=None)
    puntos[0]["items"][0].pop("linea_id")
    resp = update_orden_handler(_update_event(orden_id, puntos), None)

    assert resp["statusCode"] == 200, resp["body"]
    assert _stock(db, item_id) == 8
    assert _linea(db, orden_id)["inventario_descontado_piezas"] == 2


def test_linea_sin_linea_id_se_concilia_por_su_clave(mock_db):
    """OS anteriores a `linea_id`: el descuento previo no se pierde ni se repite."""
    db = mock_db[f"t_{TENANT}"]
    item_id = _seed_item(db, stock=10)
    orden_id = _seed_orden(db, item_id, piezas=2, linea_id=None)
    # Simula el formato viejo: booleano sin contador de piezas.
    db["ordenes_servicio"].update_one(
        {"_id": ObjectId(orden_id)},
        {"$set": {
            "puntosArreglar.0.items.0.entregado": True,
            "puntosArreglar.0.items.0.inventario_descontado": True,
        }})
    db["items"].update_one({"_id": ObjectId(item_id)}, {"$set": {"stock": 8}})

    puntos = _puntos_con(item_id, linea_id=None)
    puntos[0]["items"][0].pop("linea_id")
    resp = update_orden_handler(_update_event(orden_id, puntos), None)

    assert resp["statusCode"] == 200, resp["body"]
    assert _stock(db, item_id) == 8  # ya estaba surtida: no se descuenta de nuevo
    assert _linea(db, orden_id)["inventario_descontado_piezas"] == 2
