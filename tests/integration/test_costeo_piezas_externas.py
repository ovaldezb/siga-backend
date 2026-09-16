"""Venta de piezas fuera de inventario y su costeo posterior.

El caso real: llega un cliente por una pieza que el taller no maneja en catálogo.
Se captura a mano en el POS (nombre, número de parte y precio de salida) y se
vende. El precio de entrada muchas veces no se sabe en ese momento — llega con la
factura del proveedor días después.

Lo que se fija aquí:
- La línea manual no toca inventario y queda marcada `costo_pendiente`.
- La mano de obra NO se marca: no tiene precio de compra.
- Al capturar el costo se actualiza `costo_unitario_snapshot`, que es de donde la
  contabilidad saca el margen, y se genera la cuenta por pagar al proveedor.
- No se puede pisar un costo ya capturado ni costear una venta cancelada.
"""
import json
from datetime import datetime

from bson import ObjectId

from src.handlers.ventas.ventas_manager import (
    actualizar_costos_handler,
    create_venta_handler,
    list_costos_pendientes_handler,
)

TENANT = "tenant-costeo"
SUCURSAL = "suc-centro"


def _db(mock_db):
    return mock_db[f"t_{TENANT.replace('-', '')}"]


def _event(**extra):
    ev = {"requestContext": {"authorizer": {"claims": {
        "custom:tenant_id": TENANT, "email": "cajero@taller.com", "sub": "u-1",
    }}}}
    ev.update(extra)
    return ev


def _linea_manual(nombre="Bomba de agua", no_parte="BA-77", precio=1800.0,
                  costo=None, proveedor_id=None, cantidad=1):
    """Como la manda el POS al capturar una pieza que no está en catálogo."""
    producto = {"id": "manual", "nombre": nombre, "no_parte": no_parte}
    linea = {
        "producto": producto,
        "nombre": nombre,
        "no_parte": no_parte,
        "cantidad": cantidad,
        "precio_unitario": precio,
        "es_externo": True,
    }
    if costo is not None:
        linea["precio_compra"] = costo
    if proveedor_id:
        linea["proveedor_id"] = proveedor_id
    return linea


def _vender(mock_db, items, **extra):
    body = {"items": items, "sucursal_id": SUCURSAL, "metodo_pago": "EFECTIVO"}
    body.update(extra)
    resp = create_venta_handler(_event(body=json.dumps(body)), None)
    assert resp["statusCode"] in (200, 201), resp["body"]
    return json.loads(resp["body"])["data"]


def _pendientes():
    resp = list_costos_pendientes_handler(_event(queryStringParameters={}), None)
    assert resp["statusCode"] == 200, resp["body"]
    return json.loads(resp["body"])["data"]


# ---------- la venta ----------

def test_pieza_manual_se_vende_sin_tocar_inventario_y_queda_por_costear(mock_db):
    _vender(mock_db, [_linea_manual()])

    venta = _db(mock_db)["ventas"].find_one({})
    linea = venta["items"][0]
    assert linea["es_externo"] is True
    assert linea["costo_unitario_snapshot"] == 0
    assert linea["costo_pendiente"] is True
    # No se creó nada en el catálogo: ése fue el problema de las "piezas paja".
    assert _db(mock_db)["items"].count_documents({}) == 0


def test_pieza_manual_con_costo_capturado_no_queda_pendiente(mock_db):
    _vender(mock_db, [_linea_manual(costo=1100.0)])

    linea = _db(mock_db)["ventas"].find_one({})["items"][0]
    assert linea["costo_unitario_snapshot"] == 1100.0
    assert not linea.get("costo_pendiente")
    assert _pendientes()["count"] == 0


def test_la_mano_de_obra_no_se_marca_por_costear(mock_db):
    """Un servicio no tiene precio de compra; marcarlo llenaría la lista de ruido."""
    servicio = {
        "producto": {"id": "manual", "nombre": "Diagnóstico", "tipo": "SERVICIO"},
        "cantidad": 1, "precio_unitario": 500.0,
    }
    _vender(mock_db, [servicio, _linea_manual()])

    pendientes = _pendientes()
    assert pendientes["count"] == 1
    assert pendientes["items"][0]["nombre"] == "Bomba de agua"


# ---------- la lista ----------

def test_la_lista_devuelve_una_fila_por_linea_con_lo_necesario_para_capturar(mock_db):
    _vender(mock_db, [
        _linea_manual(nombre="Bomba de agua", no_parte="BA-77", precio=1800.0, cantidad=2),
        _linea_manual(nombre="Banda", no_parte="BD-10", precio=600.0),
    ])

    data = _pendientes()
    assert data["count"] == 2
    assert data["importe_venta_total"] == 1800.0 * 2 + 600.0

    fila = next(f for f in data["items"] if f["nombre"] == "Bomba de agua")
    assert fila["no_parte"] == "BA-77"
    assert fila["cantidad"] == 2
    assert fila["importe_venta"] == 3600.0
    assert fila["item_idx"] == 0            # con qué línea se captura
    assert fila["folio"]
    assert fila["venta_id"]


def test_la_lista_ignora_ventas_canceladas(mock_db):
    _vender(mock_db, [_linea_manual()])
    _db(mock_db)["ventas"].update_one({}, {"$set": {"estado": "CANCELADA"}})
    assert _pendientes()["count"] == 0


# ---------- el costeo ----------

def test_capturar_el_costo_actualiza_el_margen_y_limpia_la_marca(mock_db):
    venta = _vender(mock_db, [_linea_manual(precio=1800.0)])
    venta_id = venta.get("id") or str(_db(mock_db)["ventas"].find_one({})["_id"])

    resp = actualizar_costos_handler(_event(
        pathParameters={"id": venta_id},
        body=json.dumps({"costos": [{"item_idx": 0, "costo_unitario": 1100.0}]}),
    ), None)
    assert resp["statusCode"] == 200, resp["body"]
    data = json.loads(resp["body"])["data"]
    assert data["actualizadas"] == 1
    assert data["pendientes_restantes"] == 0

    linea = _db(mock_db)["ventas"].find_one({"_id": ObjectId(venta_id)})["items"][0]
    # `costo_unitario_snapshot` es de donde la contabilidad saca el margen.
    assert linea["costo_unitario_snapshot"] == 1100.0
    assert linea["costo_pendiente"] is False
    assert linea["costeo"]["capturado_por"] == "cajero@taller.com"
    assert _pendientes()["count"] == 0


def test_costear_con_proveedor_genera_la_cuenta_por_pagar(mock_db):
    proveedor_id = str(_db(mock_db)["proveedores"].insert_one({
        "nombre": "Refacciones del Centro", "rfc": "RCE010101AAA",
    }).inserted_id)

    _vender(mock_db, [_linea_manual(precio=1800.0, proveedor_id=proveedor_id, cantidad=2)])
    venta_id = str(_db(mock_db)["ventas"].find_one({})["_id"])

    # Antes de costear no se le debe nada a nadie: no se sabe cuánto.
    assert _db(mock_db)["compras"].count_documents({}) == 0

    resp = actualizar_costos_handler(_event(
        pathParameters={"id": venta_id},
        body=json.dumps({"costos": [
            {"item_idx": 0, "costo_unitario": 1100.0, "proveedor_id": proveedor_id},
        ]}),
    ), None)
    assert resp["statusCode"] == 200, resp["body"]

    compra = _db(mock_db)["compras"].find_one({})
    assert compra is not None
    assert compra["proveedor_snapshot"]["nombre"] == "Refacciones del Centro"
    assert compra["subtotal"] == 2200.0                 # 2 piezas × 1100
    assert compra["saldo_pendiente"] == compra["total"]
    assert compra["origen"] == "COSTEO_VENTA"
    assert compra["venta_id"] == venta_id
    # Marcada para que el P&L no reste dos veces el mismo costo.
    assert compra["items"][0]["en_costo_venta"] is True
    assert compra["items"][0]["afecta_inventario"] is False


def test_sin_proveedor_no_se_genera_compra(mock_db):
    _vender(mock_db, [_linea_manual()])
    venta_id = str(_db(mock_db)["ventas"].find_one({})["_id"])

    actualizar_costos_handler(_event(
        pathParameters={"id": venta_id},
        body=json.dumps({"costos": [{"item_idx": 0, "costo_unitario": 900.0}]}),
    ), None)

    assert _db(mock_db)["compras"].count_documents({}) == 0


def test_no_se_pisa_un_costo_ya_capturado(mock_db):
    _vender(mock_db, [_linea_manual(costo=1100.0)])
    venta_id = str(_db(mock_db)["ventas"].find_one({})["_id"])

    resp = actualizar_costos_handler(_event(
        pathParameters={"id": venta_id},
        body=json.dumps({"costos": [{"item_idx": 0, "costo_unitario": 5.0}]}),
    ), None)

    assert resp["statusCode"] == 400
    linea = _db(mock_db)["ventas"].find_one({})["items"][0]
    assert linea["costo_unitario_snapshot"] == 1100.0


def test_no_se_puede_costear_una_venta_cancelada(mock_db):
    _vender(mock_db, [_linea_manual()])
    venta_id = str(_db(mock_db)["ventas"].find_one({})["_id"])
    _db(mock_db)["ventas"].update_one({}, {"$set": {"estado": "CANCELADA"}})

    resp = actualizar_costos_handler(_event(
        pathParameters={"id": venta_id},
        body=json.dumps({"costos": [{"item_idx": 0, "costo_unitario": 900.0}]}),
    ), None)
    assert resp["statusCode"] == 409


def test_indices_fuera_de_rango_o_negativos_no_rompen(mock_db):
    _vender(mock_db, [_linea_manual()])
    venta_id = str(_db(mock_db)["ventas"].find_one({})["_id"])

    resp = actualizar_costos_handler(_event(
        pathParameters={"id": venta_id},
        body=json.dumps({"costos": [
            {"item_idx": 99, "costo_unitario": 100.0},
            {"item_idx": -1, "costo_unitario": 100.0},
            {"item_idx": 0, "costo_unitario": -50.0},
            {"item_idx": "x", "costo_unitario": "y"},
        ]}),
    ), None)

    assert resp["statusCode"] == 400
    assert _db(mock_db)["ventas"].find_one({})["items"][0]["costo_pendiente"] is True


def test_venta_inexistente_es_404(mock_db):
    resp = actualizar_costos_handler(_event(
        pathParameters={"id": str(ObjectId())},
        body=json.dumps({"costos": [{"item_idx": 0, "costo_unitario": 100.0}]}),
    ), None)
    assert resp["statusCode"] == 404
