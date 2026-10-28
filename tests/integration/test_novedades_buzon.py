"""Novedades de la plataforma y buzón de quejas/sugerencias."""
import json
from src.handlers.admin.novedades_manager import (
    list_novedades_handler, create_novedad_handler, update_novedad_handler, marcar_novedades_leidas_handler,
)
from src.handlers.admin.buzon_manager import create_buzon_handler, list_buzon_handler, update_buzon_handler


def _ev(sub, grupo, body=None, path=None, qs=None, tenant="tallerA"):
    claims = {"sub": sub, "cognito:groups": grupo, "name": f"Usuario {sub}", "email": f"{sub}@x.mx"}
    if tenant:
        claims["custom:tenant_id"] = tenant
    e = {"requestContext": {"authorizer": {"claims": claims}}}
    if body is not None:
        e["body"] = json.dumps(body)
    if path:
        e["pathParameters"] = path
    if qs:
        e["queryStringParameters"] = qs
    return e


def _data(resp):
    return json.loads(resp["body"])["data"]


NOVEDAD = {"titulo": "Número económico", "descripcion": "Ya puedes capturarlo en tus vehículos.", "tipo": "FEATURE"}


def test_solo_super_admin_publica(mock_db):
    assert create_novedad_handler(_ev("a1", "ADMIN", NOVEDAD), None)["statusCode"] == 403
    assert create_novedad_handler(_ev("s1", "SUPER_ADMIN", {**NOVEDAD, "tipo": "OTRO"}), None)["statusCode"] == 400
    assert create_novedad_handler(_ev("s1", "SUPER_ADMIN", NOVEDAD), None)["statusCode"] == 201


def test_cada_usuario_marca_las_suyas_y_ocultar_la_retira(mock_db):
    nid = _data(create_novedad_handler(_ev("s1", "SUPER_ADMIN", NOVEDAD), None))["id"]

    d = _data(list_novedades_handler(_ev("a1", "ADMIN"), None))
    assert d["sin_leer"] == 1 and d["items"][0]["leida"] is False

    assert marcar_novedades_leidas_handler(_ev("a1", "ADMIN", {"ids": [nid]}), None)["statusCode"] == 200
    assert _data(list_novedades_handler(_ev("a1", "ADMIN"), None))["sin_leer"] == 0
    # Otro usuario (de otro taller) la sigue teniendo pendiente.
    assert _data(list_novedades_handler(_ev("c1", "CAJERO", tenant="tallerB"), None))["sin_leer"] == 1

    update_novedad_handler(_ev("s1", "SUPER_ADMIN", {"activa": False}, {"id": nid}), None)
    assert _data(list_novedades_handler(_ev("c1", "CAJERO", tenant="tallerB"), None))["items"] == []
    # El operador la sigue viendo para poder reactivarla.
    assert len(_data(list_novedades_handler(_ev("s1", "SUPER_ADMIN", qs={"todas": "1"}), None))["items"]) == 1


def test_aviso_dirigido_solo_lo_ve_su_taller(mock_db):
    mock_db["_platform"]["talleres"].insert_one({"tenantId": "tallerA", "nombreComercial": "Bajito's"})
    # Un taller que no existe se rechaza para no publicar un aviso que nadie verá.
    assert create_novedad_handler(_ev("s1", "SUPER_ADMIN", {**NOVEDAD, "tenant_ids": ["fantasma"]}), None)["statusCode"] == 400

    resp = create_novedad_handler(_ev("s1", "SUPER_ADMIN", {**NOVEDAD, "tenant_ids": ["tallerA"]}), None)
    assert resp["statusCode"] == 201 and _data(resp)["tenant_ids"] == ["tallerA"]

    assert len(_data(list_novedades_handler(_ev("a1", "ADMIN"), None))["items"]) == 1
    assert _data(list_novedades_handler(_ev("c1", "CAJERO", tenant="tallerB"), None))["items"] == []


def test_buzon_flujo_completo(mock_db):
    mock_db["_platform"]["talleres"].insert_one({"tenantId": "tallerA", "nombreComercial": "Express"})
    resp = create_buzon_handler(_ev("a1", "ASESOR", {
        "tipo": "SUGERENCIA", "asunto": "Reporte por mecánico", "mensaje": "Estaría bien filtrar por mecánico."}), None)
    assert resp["statusCode"] == 201
    msg = _data(resp)
    assert msg["taller_nombre"] == "Express" and msg["estado"] == "NUEVO"

    # Otro usuario no lo ve; el operador sí.
    assert _data(list_buzon_handler(_ev("b1", "ADMIN"), None))["items"] == []
    bandeja = _data(list_buzon_handler(_ev("s1", "SUPER_ADMIN"), None))
    assert bandeja["nuevos"] == 1

    # Un usuario normal no puede cambiar estado ni responder.
    assert update_buzon_handler(_ev("a1", "ASESOR", {"estado": "RESUELTO"}, {"id": msg["id"]}), None)["statusCode"] == 403

    r = _data(update_buzon_handler(_ev("s1", "SUPER_ADMIN", {"respuesta": "Lo agregamos en la próxima versión."},
                                        {"id": msg["id"]}), None))
    assert r["estado"] == "RESUELTO" and r["respuesta_leida"] is False

    mios = _data(list_buzon_handler(_ev("a1", "ASESOR"), None))
    assert mios["respuestas_sin_leer"] == 1
    # Sólo el autor puede marcarla leída.
    assert update_buzon_handler(_ev("b1", "ADMIN", {"respuesta_leida": True}, {"id": msg["id"]}), None)["statusCode"] == 404
    update_buzon_handler(_ev("a1", "ASESOR", {"respuesta_leida": True}, {"id": msg["id"]}), None)
    assert _data(list_buzon_handler(_ev("a1", "ASESOR"), None))["respuestas_sin_leer"] == 0


def test_buzon_valida_y_limita(mock_db):
    assert create_buzon_handler(_ev("a1", "ADMIN", {"tipo": "X", "asunto": "a", "mensaje": "b"}), None)["statusCode"] == 400
    assert create_buzon_handler(_ev("a1", "ADMIN", {"tipo": "QUEJA", "asunto": "", "mensaje": "b"}), None)["statusCode"] == 400
    for _ in range(10):
        create_buzon_handler(_ev("a1", "ADMIN", {"tipo": "QUEJA", "asunto": "a", "mensaje": "b"}), None)
    assert create_buzon_handler(_ev("a1", "ADMIN", {"tipo": "QUEJA", "asunto": "a", "mensaje": "b"}), None)["statusCode"] == 429
