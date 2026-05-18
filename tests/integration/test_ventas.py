"""Smoke tests del flujo de ventas (POS y desde OS) — auditoría #6 fase 1."""
import json
from bson import ObjectId
from src.handlers.ventas.ventas_manager import create_venta_handler


SUCURSAL_A = "suc-a"
SUCURSAL_B = "suc-b"
TENANT = "tallertest"


def _claims():
    return {"custom:tenant_id": TENANT, "sub": "user-1", "email": "test@taller.com", "name": "Tester"}


def _seed_item(db, *, sucursal_id=SUCURSAL_A, stock=10, precio=116.0, costo=50.0, nombre="Filtro de aceite"):
    res = db["items"].insert_one({
        "nombre": nombre,
        "no_parte": f"NP-{nombre[:3].upper()}",
        "sucursal_id": sucursal_id,
        "stock": stock,
        "precio_compra": costo,
        "costo_promedio": costo,
        "precio_venta": precio,
        "maneja_inventario": True,
        "tipo": "REFACCION",
        "tenant_id": TENANT,
    })
    return str(res.inserted_id)


def _venta_event(item_id, *, sucursal_id=SUCURSAL_A, cantidad=1, precio=116.0,
                 cliente_id="PUBLICO_GENERAL", metodo_pago="EFECTIVO",
                 orden_id=None, pagos=None):
    body = {
        "sucursal_id": sucursal_id,
        "cliente_id": cliente_id,
        "items": [{
            "producto": {"id": item_id, "nombre": "Filtro de aceite", "tipo": "REFACCION"},
            "cantidad": cantidad,
            "precio_unitario": precio,
        }],
        "metodo_pago": metodo_pago,
        "pagos": pagos or [{"metodo": metodo_pago, "monto": precio * cantidad}],
    }
    if orden_id:
        body["orden_id"] = orden_id
    return {"body": json.dumps(body), "requestContext": {"authorizer": {"claims": _claims()}}}


def test_venta_pos_happy_path_descuenta_stock(mock_db):
    """Venta de mostrador sin OS: descuenta stock, calcula IVA y devuelve folio."""
    db = mock_db[f"t_{TENANT}"]
    item_id = _seed_item(db, stock=10)

    resp = create_venta_handler(_venta_event(item_id, cantidad=2, precio=116.0), None)
    assert resp["statusCode"] == 201, resp["body"]

    data = json.loads(resp["body"])["data"]
    assert data["folio"].startswith("V-")
    assert data["total"] == 232.0
    assert abs(data["subtotal"] - 200.0) < 0.01  # precio_incluye_iva=True → base = 200
    assert abs(data["iva"] - 32.0) < 0.01

    item = db["items"].find_one({"_id": ObjectId(item_id)})
    assert item["stock"] == 8


def test_venta_desde_os_cierra_orden(mock_db):
    """Venta vinculada a una OS: la OS pasa a ENTREGADO y queda pagada."""
    db = mock_db[f"t_{TENANT}"]
    item_id = _seed_item(db, stock=5)

    os_res = db["ordenes_servicio"].insert_one({
        "folio": "OS-0001",
        "tenant_id": TENANT,
        "sucursal_id": SUCURSAL_A,
        "estado": "APROBADO",
        "vehiculo_id": None,
    })
    orden_id = str(os_res.inserted_id)

    resp = create_venta_handler(_venta_event(item_id, cantidad=1, orden_id=orden_id), None)
    assert resp["statusCode"] == 201, resp["body"]

    os_after = db["ordenes_servicio"].find_one({"_id": os_res.inserted_id})
    assert os_after["estado"] == "ENTREGADO"
    assert os_after["pagada"] is True
    assert os_after["venta_folio"].startswith("V-")


def test_venta_idempotencia_misma_os(mock_db):
    """Segundo intento sobre la misma OS responde 409 con el folio original."""
    db = mock_db[f"t_{TENANT}"]
    item_id = _seed_item(db, stock=5)

    os_res = db["ordenes_servicio"].insert_one({
        "folio": "OS-0002",
        "tenant_id": TENANT,
        "sucursal_id": SUCURSAL_A,
        "estado": "APROBADO",
    })
    orden_id = str(os_res.inserted_id)

    primera = create_venta_handler(_venta_event(item_id, cantidad=1, orden_id=orden_id), None)
    assert primera["statusCode"] == 201

    segunda = create_venta_handler(_venta_event(item_id, cantidad=1, orden_id=orden_id), None)
    assert segunda["statusCode"] == 409
    body2 = json.loads(segunda["body"])
    assert "ya tiene una venta" in body2["message"]
    assert body2["data"]["folio"].startswith("V-")


def test_venta_no_consume_stock_de_otra_sucursal(mock_db):
    """Bug histórico de stock multi-sucursal: vender en B no debe descontar inventario de A."""
    db = mock_db[f"t_{TENANT}"]
    # Sólo existe stock en sucursal A. Intentamos venderlo en la B.
    item_id = _seed_item(db, sucursal_id=SUCURSAL_A, stock=10)

    resp = create_venta_handler(_venta_event(item_id, sucursal_id=SUCURSAL_B, cantidad=1), None)
    assert resp["statusCode"] == 409, resp["body"]
    body = json.loads(resp["body"])
    assert "Stock insuficiente" in body["message"]

    item_a = db["items"].find_one({"_id": ObjectId(item_id)})
    assert item_a["stock"] == 10  # no se tocó


def test_venta_sin_items_rechaza(mock_db):
    """Body sin items → 400 explícito."""
    evt = {
        "body": json.dumps({"sucursal_id": SUCURSAL_A, "items": []}),
        "requestContext": {"authorizer": {"claims": _claims()}},
    }
    resp = create_venta_handler(evt, None)
    assert resp["statusCode"] == 400
    assert "al menos un item" in json.loads(resp["body"])["message"]
