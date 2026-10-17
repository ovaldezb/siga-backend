"""Hoja de recepción configurable (indicadores del tablero y accesorios)."""
import json
from src.handlers.admin.configuracion_manager import update_config_handler

TENANT = "tallertest"


def _event(body):
    claims = {"custom:tenant_id": TENANT, "sub": "u1", "cognito:groups": "ADMIN"}
    return {"requestContext": {"authorizer": {"claims": claims}}, "body": json.dumps(body)}


def test_guarda_recepcion_normalizada(mock_db):
    resp = update_config_handler(_event({"recepcion": {
        "testigos": [
            {"nombre": "  Motor ", "icono": "motor.png", "activo": True},
            {"nombre": "DPF", "icono": "../../etc/passwd", "activo": True},
            {"nombre": "ABS", "icono": "abs.png", "activo": False},
            {"nombre": "   "},
        ],
        "accesorios": [{"nombre": "Antena"}, {"nombre": "Tapetes", "activo": False}],
    }}), None)
    assert resp["statusCode"] == 200, resp["body"]

    rec = mock_db[f"t_{TENANT}"]["configuracion"].find_one({"tenant_id": TENANT})["recepcion"]
    assert rec["testigos"] == [
        {"nombre": "Motor", "activo": True, "icono": "motor.png"},
        {"nombre": "DPF", "activo": True, "icono": None},
        {"nombre": "ABS", "activo": False, "icono": "abs.png"},
    ]
    assert rec["accesorios"] == [{"nombre": "Antena", "activo": True}, {"nombre": "Tapetes", "activo": False}]


def test_rechaza_repetidos_y_formas_invalidas(mock_db):
    dup = update_config_handler(_event({"recepcion": {"accesorios": [{"nombre": "Gato"}, {"nombre": "gato"}]}}), None)
    assert dup["statusCode"] == 400
    assert update_config_handler(_event({"recepcion": {"testigos": "Motor"}}), None)["statusCode"] == 400
    assert update_config_handler(_event({"recepcion": {"testigos": [{"nombre": "x" * 41}]}}), None)["statusCode"] == 400
