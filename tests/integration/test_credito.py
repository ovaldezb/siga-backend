"""Smoke tests del flujo de crédito y abonos — auditoría #6 fase 1."""
import json
from bson import ObjectId
from src.handlers.ventas.ventas_manager import create_venta_handler, registrar_abono_handler


SUCURSAL = "suc-a"
TENANT = "tallertest"


def _claims():
    return {"custom:tenant_id": TENANT, "sub": "user-1", "email": "test@taller.com", "name": "Tester"}


def _seed_item(db, stock=10, precio=116.0):
    res = db["items"].insert_one({
        "nombre": "Filtro",
        "no_parte": "NP-FIL",
        "sucursal_id": SUCURSAL,
        "stock": stock,
        "precio_compra": 50.0,
        "costo_promedio": 50.0,
        "precio_venta": precio,
        "maneja_inventario": True,
        "tenant_id": TENANT,
    })
    return str(res.inserted_id)


def _seed_cliente(db, *, limite_credito=1000.0, nombre="Juan Cliente"):
    res = db["clientes"].insert_one({
        "nombre": nombre,
        "limite_credito": limite_credito,
        "tenant_id": TENANT,
    })
    return str(res.inserted_id)


def _venta_credito_event(item_id, cliente_id, *, cantidad=1, precio=116.0, monto_credito=None):
    monto = monto_credito if monto_credito is not None else precio * cantidad
    body = {
        "sucursal_id": SUCURSAL,
        "cliente_id": cliente_id,
        "items": [{
            "producto": {"id": item_id, "nombre": "Filtro", "tipo": "REFACCION"},
            "cantidad": cantidad,
            "precio_unitario": precio,
        }],
        "metodo_pago": "CREDITO",
        "pagos": [{"metodo": "CREDITO", "monto": monto}],
    }
    return {"body": json.dumps(body), "requestContext": {"authorizer": {"claims": _claims()}}}


def test_venta_credito_sin_cliente_rechaza(mock_db):
    """Crédito a PUBLICO_GENERAL → 400."""
    db = mock_db[f"t_{TENANT}"]
    item_id = _seed_item(db)
    resp = create_venta_handler(_venta_credito_event(item_id, "PUBLICO_GENERAL"), None)
    assert resp["statusCode"] == 400
    assert "cliente registrado" in json.loads(resp["body"])["message"]


def test_venta_credito_dentro_de_limite_ok(mock_db):
    """Venta a crédito por 500 con límite 1000 sin saldo previo → OK + saldo_pendiente=500."""
    db = mock_db[f"t_{TENANT}"]
    item_id = _seed_item(db, stock=10, precio=500.0)
    cliente_id = _seed_cliente(db, limite_credito=1000.0)

    resp = create_venta_handler(_venta_credito_event(item_id, cliente_id, precio=500.0), None)
    assert resp["statusCode"] == 201, resp["body"]
    data = json.loads(resp["body"])["data"]
    assert data["monto_credito"] == 500.0
    assert data["saldo_pendiente"] == 500.0


def test_venta_credito_excede_limite_rechaza(mock_db):
    """Saldo previo 700 + venta 400 con límite 1000 → 400 'Crédito insuficiente'."""
    db = mock_db[f"t_{TENANT}"]
    item_id = _seed_item(db, stock=10, precio=400.0)
    cliente_id = _seed_cliente(db, limite_credito=1000.0)

    # Seed saldo previo: una venta a crédito anterior con saldo_pendiente=700
    db["ventas"].insert_one({
        "cliente_id": cliente_id,
        "saldo_pendiente": 700.0,
        "total": 700.0,
        "sucursal_id": SUCURSAL,
        "tenant_id": TENANT,
    })

    resp = create_venta_handler(_venta_credito_event(item_id, cliente_id, precio=400.0), None)
    assert resp["statusCode"] == 400
    msg = json.loads(resp["body"])["message"]
    assert "Crédito insuficiente" in msg
    assert "Disponible" in msg


def test_abono_baja_saldo_pendiente(mock_db):
    """Abono de 200 contra venta con saldo 500 → saldo queda en 300."""
    db = mock_db[f"t_{TENANT}"]
    item_id = _seed_item(db, stock=10, precio=500.0)
    cliente_id = _seed_cliente(db, limite_credito=1000.0)

    # Crear venta a crédito
    venta_resp = create_venta_handler(_venta_credito_event(item_id, cliente_id, precio=500.0), None)
    assert venta_resp["statusCode"] == 201
    venta_id = json.loads(venta_resp["body"])["data"]["id"]

    abono_evt = {
        "pathParameters": {"id": venta_id},
        "body": json.dumps({"monto": 200, "metodo": "EFECTIVO"}),
        "requestContext": {"authorizer": {"claims": _claims()}},
    }
    abono_resp = registrar_abono_handler(abono_evt, None)
    assert abono_resp["statusCode"] == 200, abono_resp["body"]
    data = json.loads(abono_resp["body"])["data"]
    assert data["saldo_pendiente"] == 300.0
    assert len(data["abonos"]) == 1
    assert data["abonos"][0]["monto"] == 200.0


def test_abono_mayor_que_saldo_rechaza(mock_db):
    """Abono de 600 contra saldo 500 → 400."""
    db = mock_db[f"t_{TENANT}"]
    item_id = _seed_item(db, stock=10, precio=500.0)
    cliente_id = _seed_cliente(db, limite_credito=1000.0)

    venta_resp = create_venta_handler(_venta_credito_event(item_id, cliente_id, precio=500.0), None)
    venta_id = json.loads(venta_resp["body"])["data"]["id"]

    abono_evt = {
        "pathParameters": {"id": venta_id},
        "body": json.dumps({"monto": 600}),
        "requestContext": {"authorizer": {"claims": _claims()}},
    }
    abono_resp = registrar_abono_handler(abono_evt, None)
    assert abono_resp["statusCode"] == 400
    assert "excede el saldo" in json.loads(abono_resp["body"])["message"]


def test_abono_que_salda_cierra_orden(mock_db):
    """Cuando el abono lleva saldo a 0 y la venta vino de una OS, la OS pasa a ENTREGADO."""
    db = mock_db[f"t_{TENANT}"]
    item_id = _seed_item(db, stock=10, precio=500.0)
    cliente_id = _seed_cliente(db, limite_credito=1000.0)

    os_res = db["ordenes_servicio"].insert_one({
        "folio": "OS-CR",
        "tenant_id": TENANT,
        "sucursal_id": SUCURSAL,
        "estado": "APROBADO",
    })
    orden_id = str(os_res.inserted_id)

    # Venta a crédito desde la OS
    evt = _venta_credito_event(item_id, cliente_id, precio=500.0)
    body = json.loads(evt["body"])
    body["orden_id"] = orden_id
    evt["body"] = json.dumps(body)
    venta_resp = create_venta_handler(evt, None)
    assert venta_resp["statusCode"] == 201, venta_resp["body"]
    venta_id = json.loads(venta_resp["body"])["data"]["id"]

    # Abono total (500)
    abono_evt = {
        "pathParameters": {"id": venta_id},
        "body": json.dumps({"monto": 500, "metodo": "EFECTIVO"}),
        "requestContext": {"authorizer": {"claims": _claims()}},
    }
    abono_resp = registrar_abono_handler(abono_evt, None)
    assert abono_resp["statusCode"] == 200, abono_resp["body"]

    os_after = db["ordenes_servicio"].find_one({"_id": os_res.inserted_id})
    assert os_after["estado"] == "ENTREGADO"
    assert os_after["pagada"] is True
    assert os_after["saldo_pendiente"] == 0
