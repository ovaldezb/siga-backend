"""Catálogo compartido entre sucursales.

Cubre las tres piezas del alta multi-sucursal:
  · crear un artículo lo da de alta en todas las sucursales (stock 0 en las demás),
  · editarlo propaga precios y datos del catálogo, pero NUNCA stock ni costos,
  · el listado puede devolver la existencia del mismo número de parte por sucursal.
"""
import json
from bson import ObjectId

from src.handlers.inventario.items_manager import (
    create_item_handler,
    update_item_handler,
    list_items_handler,
)

TENANT = "taller_test"
DB_NAME = "t_taller_test"


def _claims(admin=True):
    claims = {"custom:tenant_id": TENANT, "email": "admin@taller.test"}
    if admin:
        claims["cognito:groups"] = "ADMIN"
    return {"requestContext": {"authorizer": {"claims": claims}}}


def _sembrar_sucursales(mock_db, cuantas=2, activas=True):
    db = mock_db[DB_NAME]
    ids = []
    for n in range(cuantas):
        res = db.sucursales.insert_one({
            "nombre": f"Sucursal {n + 1}",
            "activa": activas,
            "tenant_id": TENANT,
        })
        ids.append(str(res.inserted_id))
    return ids


def _crear(sucursal_id, **extra):
    body = {
        "tipo": "PRODUCTO",
        "no_parte": "FIL-100",
        "nombre": "Filtro de aceite",
        "precio_venta": 250,
        "precio_compra": 100,
        "stock": 8,
        "maneja_inventario": True,
        "sucursalId": sucursal_id,
    }
    body.update(extra)
    return create_item_handler({**_claims(), "body": json.dumps(body)}, None)


def test_alta_replica_el_numero_de_parte_en_las_demas_sucursales(mock_db):
    matriz, sur = _sembrar_sucursales(mock_db)
    db = mock_db[DB_NAME]

    resp = _crear(matriz)
    assert resp["statusCode"] == 201

    clones = list(db.items.find({"no_parte": "FIL-100"}))
    assert len(clones) == 2, "debe existir en ambas sucursales"

    clon = next(c for c in clones if c["sucursal_id"] == sur)
    assert clon["stock"] == 0, "la sucursal destino arranca sin existencia"
    assert clon["precio_venta"] == 250
    assert clon["clonado_de"]

    original = next(c for c in clones if c["sucursal_id"] == matriz)
    assert original["stock"] == 8


def test_alta_respeta_un_numero_de_parte_ya_existente_en_el_destino(mock_db):
    matriz, sur = _sembrar_sucursales(mock_db)
    db = mock_db[DB_NAME]
    db.items.insert_one({
        "tipo": "PRODUCTO", "no_parte": "FIL-100", "nombre": "Filtro viejo",
        "precio_venta": 99, "stock": 3, "maneja_inventario": True,
        "sucursal_id": sur, "tenant_id": TENANT,
    })

    _crear(matriz)

    en_sur = list(db.items.find({"sucursal_id": sur}))
    assert len(en_sur) == 1
    assert en_sur[0]["precio_venta"] == 99, "no se pisa el artículo local"
    assert en_sur[0]["stock"] == 3


def test_alta_sin_replicar_cuando_se_pide_explicitamente(mock_db):
    matriz, _ = _sembrar_sucursales(mock_db)
    db = mock_db[DB_NAME]

    _crear(matriz, replicar_en_sucursales=False)

    assert db.items.count_documents({"no_parte": "FIL-100"}) == 1


def test_editar_propaga_precios_pero_no_stock_ni_costos(mock_db):
    matriz, sur = _sembrar_sucursales(mock_db)
    db = mock_db[DB_NAME]
    _crear(matriz)

    # El clon vive su propia vida en la otra sucursal: recibe mercancía y costo.
    db.items.update_one({"sucursal_id": sur},
                        {"$set": {"stock": 12, "costo_promedio": 130, "precio_compra": 130}})

    item_matriz = db.items.find_one({"sucursal_id": matriz})
    resp = update_item_handler({
        **_claims(),
        "pathParameters": {"id": str(item_matriz["_id"])},
        "body": json.dumps({"precio_venta": 320, "marca": "WIX", "sucursalId": matriz}),
    }, None)
    assert resp["statusCode"] == 200

    clon = db.items.find_one({"sucursal_id": sur})
    assert clon["precio_venta"] == 320, "el precio se captura una sola vez"
    assert clon["marca"] == "WIX"
    assert clon["stock"] == 12, "la existencia es local de cada sucursal"
    assert clon["costo_promedio"] == 130, "el costo es local de cada sucursal"


def test_editar_sincroniza_el_numero_de_parte_cuando_cambia(mock_db):
    matriz, sur = _sembrar_sucursales(mock_db)
    db = mock_db[DB_NAME]
    _crear(matriz)

    item_matriz = db.items.find_one({"sucursal_id": matriz})
    update_item_handler({
        **_claims(),
        "pathParameters": {"id": str(item_matriz["_id"])},
        "body": json.dumps({"no_parte": "FIL-200", "precio_venta": 300, "sucursalId": matriz}),
    }, None)

    assert db.items.count_documents({"no_parte": "FIL-200"}) == 2
    assert db.items.count_documents({"no_parte": "FIL-100"}) == 0


def test_editar_sin_propagar_cuando_se_pide_explicitamente(mock_db):
    matriz, sur = _sembrar_sucursales(mock_db)
    db = mock_db[DB_NAME]
    _crear(matriz)

    item_matriz = db.items.find_one({"sucursal_id": matriz})
    update_item_handler({
        **_claims(),
        "pathParameters": {"id": str(item_matriz["_id"])},
        "body": json.dumps({"precio_venta": 999, "sucursalId": matriz,
                            "propagar_a_sucursales": False}),
    }, None)

    assert db.items.find_one({"sucursal_id": sur})["precio_venta"] == 250


def test_listado_devuelve_existencias_de_todas_las_sucursales(mock_db):
    matriz, sur = _sembrar_sucursales(mock_db)
    db = mock_db[DB_NAME]
    _crear(matriz)
    db.items.update_one({"sucursal_id": sur}, {"$set": {"stock": 4}})

    resp = list_items_handler({
        **_claims(),
        "queryStringParameters": {"sucursalId": matriz, "existencias": "true"},
    }, None)
    assert resp["statusCode"] == 200

    data = json.loads(resp["body"])["data"]
    item = data["items"][0]
    por_sucursal = {e["sucursal_id"]: e["stock"] for e in item["existencias"]}
    assert por_sucursal == {matriz: 8, sur: 4}
    assert item["stock_total"] == 12
    assert {s["id"] for s in data["sucursales"]} == {matriz, sur}


def test_listado_sin_existencias_no_agrega_el_campo(mock_db):
    matriz, _ = _sembrar_sucursales(mock_db)
    _crear(matriz)

    resp = list_items_handler({
        **_claims(),
        "queryStringParameters": {"sucursalId": matriz},
    }, None)

    item = json.loads(resp["body"])["data"]["items"][0]
    assert "existencias" not in item
