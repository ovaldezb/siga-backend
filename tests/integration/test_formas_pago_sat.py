"""Forma de pago SAT de los métodos de pago: resolución, migración de la config y ventas."""
import json
from src.shared.utils.formas_pago_sat import (
    resolver_forma_pago_sat, completar_codigos_config, deduplicar_ids, codigo_por_nombre,
)
from src.handlers.admin.configuracion_manager import get_config_handler, update_config_handler
from src.handlers.ventas.ventas_manager import create_venta_handler

TENANT = "tallertest"


def _event(body=None, admin=True):
    claims = {"custom:tenant_id": TENANT, "sub": "user-1", "email": "test@taller.com", "name": "Tester"}
    if admin:
        claims["cognito:groups"] = "ADMIN"
        claims["custom:role"] = "ADMIN"
    ev = {"requestContext": {"authorizer": {"claims": claims}}}
    if body is not None:
        ev["body"] = json.dumps(body)
    return ev


def test_codigo_por_nombre():
    assert codigo_por_nombre("Efectivo") == "01"
    assert codigo_por_nombre("TARJETA") == "04"
    assert codigo_por_nombre("Tarjeta de Crédito") == "04"
    assert codigo_por_nombre("Tarjeta debito ") == "28"
    assert codigo_por_nombre("Transferencia SPEI") == "03"
    assert codigo_por_nombre("CREDITO") == "99"
    assert codigo_por_nombre("Crédito") == "99"
    assert codigo_por_nombre("Cheque") == "02"
    assert codigo_por_nombre("Clip") == ""


def test_resolver_usa_config_antes_que_nombre():
    cfg = [
        {"id": "tarjeta", "nombre": "Tarjeta", "codigo_sat": "28"},
        {"id": "1787163598147", "nombre": "Vales", "codigo_sat": "08"},
    ]
    assert resolver_forma_pago_sat("tarjeta", cfg) == "28"
    assert resolver_forma_pago_sat("TARJETA", cfg) == "28"
    assert resolver_forma_pago_sat("1787163598147", cfg) == "08"
    assert resolver_forma_pago_sat("VALES", cfg) == "08"
    assert resolver_forma_pago_sat("efectivo", cfg) == "01"
    assert resolver_forma_pago_sat("", cfg) == ""


def test_completar_no_pisa_codigos_capturados():
    metodos = [
        {"id": "efectivo", "nombre": "Efectivo", "codigo_sat": "01"},
        {"id": "tarjeta", "nombre": "Tarjeta", "codigo_sat": "28"},
        {"id": "transferencia", "nombre": "Transferencia"},
        {"id": "credito", "nombre": "Crédito", "codigo_sat": ""},
    ]
    salida, cambio = completar_codigos_config(metodos)
    assert cambio
    assert [m["codigo_sat"] for m in salida] == ["01", "28", "03", "99"]
    _, cambio2 = completar_codigos_config(salida)
    assert not cambio2


def test_deduplicar_ids():
    metodos = [
        {"id": "tarjeta", "nombre": "Tarjeta de credito", "codigo_sat": "04"},
        {"id": "tarjeta", "nombre": "Tarjeta de débito", "codigo_sat": "28"},
    ]
    salida, cambio = deduplicar_ids(metodos)
    assert cambio
    assert salida[0]["id"] == "tarjeta"
    assert salida[1]["id"] == "tarjeta_de_debito"
    assert salida[1]["codigo_sat"] == "28"


def test_get_config_rellena_codigos_de_taller_viejo(mock_db):
    db = mock_db[f"t_{TENANT}"]
    db["configuracion"].insert_one({"tenant_id": TENANT, "metodos_pago": [
        {"id": "efectivo", "nombre": "Efectivo", "codigo_sat": "01"},
        {"id": "tarjeta", "nombre": "Tarjeta"},
        {"id": "credito", "nombre": "Crédito"},
    ]})
    resp = get_config_handler(_event(), None)
    assert resp["statusCode"] == 200
    data = json.loads(resp["body"])["data"]
    assert [m["codigo_sat"] for m in data["metodos_pago"]] == ["01", "04", "99"]
    guardado = db["configuracion"].find_one({"tenant_id": TENANT})
    assert [m["codigo_sat"] for m in guardado["metodos_pago"]] == ["01", "04", "99"]


def test_get_config_nuevo_trae_codigos(mock_db):
    resp = get_config_handler(_event(), None)
    data = json.loads(resp["body"])["data"]
    assert {m["id"]: m["codigo_sat"] for m in data["metodos_pago"]} == {
        "efectivo": "01", "tarjeta": "04", "transferencia": "03", "credito": "99"}


def test_update_config_rechaza_codigo_fuera_de_catalogo(mock_db):
    body = {"metodos_pago": [{"id": "x", "nombre": "Raro", "codigo_sat": "77"}]}
    resp = update_config_handler(_event(body), None)
    assert resp["statusCode"] == 400
    assert "77" in json.loads(resp["body"])["message"]


def test_update_config_completa_codigo_vacio(mock_db):
    db = mock_db[f"t_{TENANT}"]
    body = {"metodos_pago": [{"id": "transferencia", "nombre": "Transferencia", "codigo_sat": ""}]}
    resp = update_config_handler(_event(body), None)
    assert resp["statusCode"] == 200
    guardado = db["configuracion"].find_one({"tenant_id": TENANT})
    assert guardado["metodos_pago"][0]["codigo_sat"] == "03"


def test_venta_sin_forma_pago_la_toma_de_la_config(mock_db):
    db = mock_db[f"t_{TENANT}"]
    db["configuracion"].insert_one({"tenant_id": TENANT, "metodos_pago": [
        {"id": "tarjeta", "nombre": "Tarjeta", "codigo_sat": "28"},
    ]})
    item = db["items"].insert_one({
        "nombre": "Filtro", "no_parte": "NP-F", "sucursal_id": "suc-a", "stock": 5,
        "precio_compra": 50.0, "costo_promedio": 50.0, "precio_venta": 100.0,
        "maneja_inventario": True, "tipo": "REFACCION", "tenant_id": TENANT,
    })
    body = {
        "sucursal_id": "suc-a", "cliente_id": "PUBLICO_GENERAL",
        "items": [{"producto": {"id": str(item.inserted_id), "nombre": "Filtro", "tipo": "REFACCION"},
                   "cantidad": 1, "precio_unitario": 100.0}],
        "metodo_pago": "tarjeta",
        "pagos": [{"metodo": "tarjeta", "monto": 100.0, "forma_pago_sat": ""}],
        "forma_pago_sat": "",
    }
    resp = create_venta_handler(_event(body, admin=False), None)
    assert resp["statusCode"] == 201, resp["body"]
    folio = json.loads(resp["body"])["data"]["folio"]
    venta = db["ventas"].find_one({"folio": folio})
    assert venta["pagos"][0]["forma_pago_sat"] == "28"
    assert venta["forma_pago_sat"] == "28"
