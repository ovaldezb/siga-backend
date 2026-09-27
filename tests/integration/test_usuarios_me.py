"""GET /usuarios/me: perfil con sucursales pobladas."""
import json

from bson import ObjectId

from src.handlers.users.user_manager import get_me_handler

TENANT = "tallertest"


def _event(**claims):
    base = {"custom:tenant_id": TENANT, "email": "admin@taller.com"}
    base.update(claims)
    return {"requestContext": {"authorizer": {"claims": {k: v for k, v in base.items() if v is not None}}}}


def test_me_puebla_sucursales_y_tolera_formato_legacy(mock_db):
    db = mock_db[f"t_{TENANT}"]
    matriz = str(db["sucursales"].insert_one({"nombre": "Matriz"}).inserted_id)
    norte = str(db["sucursales"].insert_one({"nombre": "Norte"}).inserted_id)
    db["usuarios"].insert_one({"email": "admin@taller.com", "sucursales": [
        {"sucursal": matriz}, norte, {"sucursal": str(ObjectId())}]})

    resp = get_me_handler(_event(), None)
    assert resp["statusCode"] == 200
    data = json.loads(resp["body"])["data"]
    assert [s["nombre"] for s in data["sucursales"]] == ["Matriz", "Norte"]


def test_me_sin_email_o_tenant(mock_db):
    assert get_me_handler(_event(email=None), None)["statusCode"] == 401
    assert get_me_handler(_event(**{"custom:tenant_id": None}), None)["statusCode"] == 403
