"""Smoke tests de traspasos entre sucursales — auditoría #6 fase 1."""
import json
from bson import ObjectId
from src.handlers.inventario.traspasos_manager import (
    create_traspaso_handler,
    receive_traspaso_handler,
)


TENANT = "tallertest"
SUCURSAL_A = "suc-a"
SUCURSAL_B = "suc-b"


def _claims():
    return {"custom:tenant_id": TENANT, "sub": "user-1", "name": "Tester"}


def _seed_item(db, *, sucursal_id, stock, nombre="Filtro", no_parte="NP-FIL"):
    res = db["items"].insert_one({
        "nombre": nombre,
        "no_parte": no_parte,
        "sucursal_id": sucursal_id,
        "stock": stock,
        "precio_compra": 50.0,
        "maneja_inventario": True,
        "tenant_id": TENANT,
    })
    return str(res.inserted_id)


def _create_event(*, origen, destino, items):
    return {
        "body": json.dumps({"origen_id": origen, "destino_id": destino, "items": items}),
        "requestContext": {"authorizer": {"claims": _claims()}},
    }


def test_traspaso_descuenta_origen_y_queda_en_transito(mock_db):
    db = mock_db[f"t_{TENANT}"]
    item_id = _seed_item(db, sucursal_id=SUCURSAL_A, stock=10)

    resp = create_traspaso_handler(_create_event(
        origen=SUCURSAL_A, destino=SUCURSAL_B,
        items=[{"item_id": item_id, "cantidad": 3}],
    ), None)
    assert resp["statusCode"] == 201, resp["body"]
    data = json.loads(resp["body"])["data"]
    assert data["estado"] == "EN_TRANSITO"
    assert len(data["items"]) == 1
    assert data["items"][0]["no_parte"] == "NP-FIL"

    item = db["items"].find_one({"_id": ObjectId(item_id)})
    assert item["stock"] == 7


def test_traspaso_stock_insuficiente_rechaza(mock_db):
    db = mock_db[f"t_{TENANT}"]
    item_id = _seed_item(db, sucursal_id=SUCURSAL_A, stock=2)

    resp = create_traspaso_handler(_create_event(
        origen=SUCURSAL_A, destino=SUCURSAL_B,
        items=[{"item_id": item_id, "cantidad": 5}],
    ), None)
    assert resp["statusCode"] == 400
    assert "Stock insuficiente" in json.loads(resp["body"])["message"]

    item = db["items"].find_one({"_id": ObjectId(item_id)})
    assert item["stock"] == 2  # nada cambió


def test_traspaso_mismo_origen_destino_rechaza(mock_db):
    db = mock_db[f"t_{TENANT}"]
    item_id = _seed_item(db, sucursal_id=SUCURSAL_A, stock=5)

    resp = create_traspaso_handler(_create_event(
        origen=SUCURSAL_A, destino=SUCURSAL_A,
        items=[{"item_id": item_id, "cantidad": 1}],
    ), None)
    assert resp["statusCode"] == 400
    assert "no pueden ser la misma" in json.loads(resp["body"])["message"]


def test_recibir_traspaso_clona_item_en_destino(mock_db):
    """Si el destino no tiene el item, se clona desde el origen con el stock recibido."""
    db = mock_db[f"t_{TENANT}"]
    item_id = _seed_item(db, sucursal_id=SUCURSAL_A, stock=10, no_parte="NP-CLON")

    # Crear traspaso
    create_resp = create_traspaso_handler(_create_event(
        origen=SUCURSAL_A, destino=SUCURSAL_B,
        items=[{"item_id": item_id, "cantidad": 4}],
    ), None)
    assert create_resp["statusCode"] == 201
    traspaso_id = json.loads(create_resp["body"])["data"]["id"]

    # Recibir COMPLETADO
    recv_evt = {
        "pathParameters": {"id": traspaso_id},
        "body": json.dumps({
            "estado": "COMPLETADO",
            "items_recibidos": [{"item_id": item_id, "cantidad_recibida": 4}],
        }),
        "requestContext": {"authorizer": {"claims": _claims()}},
    }
    recv_resp = receive_traspaso_handler(recv_evt, None)
    assert recv_resp["statusCode"] == 200, recv_resp["body"]

    # Destino debería tener un item clonado con stock=4
    clon = db["items"].find_one({"no_parte": "NP-CLON", "sucursal_id": SUCURSAL_B})
    assert clon is not None
    assert clon["stock"] == 4
    assert clon.get("clonado_de") == item_id

    # Origen sigue con 6 (10 - 4 descontados al crear el traspaso)
    origen = db["items"].find_one({"_id": ObjectId(item_id)})
    assert origen["stock"] == 6

    # Traspaso quedó en COMPLETADO
    traspaso = db["traspasos"].find_one({"_id": ObjectId(traspaso_id)})
    assert traspaso["estado"] == "COMPLETADO"


def test_recibir_traspaso_reconcilia_por_no_parte(mock_db):
    """Si el destino ya tiene el mismo no_parte (otro _id), suma stock en vez de clonar."""
    db = mock_db[f"t_{TENANT}"]
    item_origen = _seed_item(db, sucursal_id=SUCURSAL_A, stock=10, no_parte="NP-RECON")
    item_destino_existente = _seed_item(db, sucursal_id=SUCURSAL_B, stock=2, no_parte="NP-RECON")

    create_resp = create_traspaso_handler(_create_event(
        origen=SUCURSAL_A, destino=SUCURSAL_B,
        items=[{"item_id": item_origen, "cantidad": 3}],
    ), None)
    traspaso_id = json.loads(create_resp["body"])["data"]["id"]

    recv_evt = {
        "pathParameters": {"id": traspaso_id},
        "body": json.dumps({
            "estado": "COMPLETADO",
            "items_recibidos": [{"item_id": item_origen, "cantidad_recibida": 3}],
        }),
        "requestContext": {"authorizer": {"claims": _claims()}},
    }
    recv_resp = receive_traspaso_handler(recv_evt, None)
    assert recv_resp["statusCode"] == 200

    # El item destino sumó 3 a sus 2 originales → 5. NO se creó un clon nuevo.
    destino = db["items"].find_one({"_id": ObjectId(item_destino_existente)})
    assert destino["stock"] == 5
    matches = list(db["items"].find({"no_parte": "NP-RECON", "sucursal_id": SUCURSAL_B}))
    assert len(matches) == 1  # sigue habiendo uno solo


def test_traspaso_a_traspaso_ya_recibido_rechaza(mock_db):
    db = mock_db[f"t_{TENANT}"]
    item_id = _seed_item(db, sucursal_id=SUCURSAL_A, stock=10)

    create_resp = create_traspaso_handler(_create_event(
        origen=SUCURSAL_A, destino=SUCURSAL_B,
        items=[{"item_id": item_id, "cantidad": 2}],
    ), None)
    traspaso_id = json.loads(create_resp["body"])["data"]["id"]

    recv_evt = {
        "pathParameters": {"id": traspaso_id},
        "body": json.dumps({
            "estado": "COMPLETADO",
            "items_recibidos": [{"item_id": item_id, "cantidad_recibida": 2}],
        }),
        "requestContext": {"authorizer": {"claims": _claims()}},
    }
    first = receive_traspaso_handler(recv_evt, None)
    assert first["statusCode"] == 200

    second = receive_traspaso_handler(recv_evt, None)
    assert second["statusCode"] == 400
    assert "no está en tránsito" in json.loads(second["body"])["message"]
