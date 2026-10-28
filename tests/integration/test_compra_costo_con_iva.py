"""El costo promedio que deja una compra es lo que se le pagó al proveedor, IVA incluido.

La operación va sin IVA (el ingreso de una venta es el `total` cobrado), así que el
costo con el que se calcula la utilidad también debe ser bruto. Antes el promedio se
guardaba neto: un acumulador comprado en $2,639 se costeaba en $2,275 al venderlo.
"""
import json
from bson import ObjectId

from src.handlers.compras.compras_manager import create_compra_handler

TENANT = "taller_test"
DB_NAME = "t_taller_test"


def _evento(body):
    claims = {"custom:tenant_id": TENANT, "email": "admin@taller.test", "cognito:groups": "ADMIN"}
    return {"requestContext": {"authorizer": {"claims": claims}}, "body": json.dumps(body)}


def _sembrar(mock_db, **item):
    db = mock_db[DB_NAME]
    suc = str(db.sucursales.insert_one({"nombre": "Matriz", "activa": True}).inserted_id)
    prov = str(db.proveedores.insert_one({"nombre": "ROCA LTH"}).inserted_id)
    base = {"nombre": "ACUMULADOR LTH", "no_parte": "L-24R-530", "sucursal_id": suc,
            "stock": 0, "precio_compra": 0, "maneja_inventario": True}
    base.update(item)
    item_id = db.items.insert_one(base).inserted_id
    return db, suc, prov, item_id


def _comprar(suc, prov, item_id, costo, cantidad, incluye_iva=True):
    return create_compra_handler(_evento({
        "proveedor_id": prov,
        "sucursal_id": suc,
        "items": [{"item_id": str(item_id), "cantidad": cantidad,
                   "costo_unitario": costo, "costo_incluye_iva": incluye_iva}],
    }), None)


def test_compra_con_iva_incluido_deja_el_costo_bruto(mock_db):
    db, suc, prov, item_id = _sembrar(mock_db)

    resp = _comprar(suc, prov, item_id, 2639, 2)
    assert resp["statusCode"] in (200, 201), resp["body"]

    item = db.items.find_one({"_id": ObjectId(item_id)})
    assert item["stock"] == 2
    assert item["costo_promedio"] == 2639
    assert item["precio_compra"] == 2639
    # El desglose fiscal de la compra no cambia.
    compra = db.compras.find_one({})
    assert compra["items"][0]["costo_unitario_neto"] == round(2639 / 1.16, 4)


def test_compra_mas_iva_promedia_con_el_costo_pagado(mock_db):
    db, suc, prov, item_id = _sembrar(mock_db, stock=1, costo_promedio=1160, precio_compra=1160)

    resp = _comprar(suc, prov, item_id, 1000, 1, incluye_iva=False)
    assert resp["statusCode"] in (200, 201), resp["body"]

    item = db.items.find_one({"_id": ObjectId(item_id)})
    assert item["costo_promedio"] == 1160
