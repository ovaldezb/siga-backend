"""Autorizaciones del POS: solo un admin resuelve y el POS ve también el rechazo."""
import json

from src.handlers.ventas.autorizaciones_manager import (
    create_autorizacion_handler,
    list_autorizaciones_handler,
    update_autorizacion_handler,
)

TENANT = "tenant-1"


def _event(grupos=None, body=None, query=None, path=None):
    claims = {"sub": "sub-cajero", "email": "cajero@taller.com", "custom:tenant_id": TENANT}
    if grupos:
        claims["cognito:groups"] = grupos
    event = {"requestContext": {"authorizer": {"claims": claims}}}
    if body is not None:
        event["body"] = json.dumps(body)
    if query:
        event["queryStringParameters"] = query
    if path:
        event["pathParameters"] = path
    return event


def _solicitud(precio=100):
    return {"sucursal_id": "suc-1", "tipo": "PRECIO_POS", "estado": "APROBADA",
            "metadata": {"producto_id": "p1", "precio_solicitado": precio}}


def _crear(precio=100):
    resp = create_autorizacion_handler(_event("[CAJERO]", _solicitud(precio)), None)
    assert resp['statusCode'] == 201
    return json.loads(resp['body'])['data']


def _resolver(auth_id, estado, grupos="[ADMIN]"):
    return update_autorizacion_handler(
        _event(grupos, {"estado": estado}, path={"id": auth_id}), None)['statusCode']


def test_create_valida_sucursal_y_precio(mock_db):
    sin_sucursal = {"metadata": {"precio_solicitado": 10}}
    assert create_autorizacion_handler(_event(body=sin_sucursal), None)['statusCode'] == 400
    for precio in (None, "cien", -5, True):
        assert create_autorizacion_handler(_event(body=_solicitud(precio)), None)['statusCode'] == 400


def test_create_ignora_estado_del_cliente(mock_db):
    auth = _crear()
    assert auth['estado'] == "PENDIENTE"
    assert auth['solicitante']['nombre'] == "cajero@taller.com"


def test_cajero_no_puede_aprobarse(mock_db):
    auth = _crear()
    assert _resolver(auth['id'], "APROBADA", grupos="[CAJERO]") == 403
    assert _resolver(auth['id'], "APROBADA", grupos=None) == 403


def test_id_invalido_es_400(mock_db):
    assert _resolver("no-es-objectid", "APROBADA") == 400


def test_pos_ve_aprobadas_y_rechazadas(mock_db):
    a = _crear(80)
    b = _crear(10)
    assert _resolver(a['id'], "APROBADA") == 200
    assert _resolver(a['id'], "RECHAZADA") == 404  # ya resuelta
    assert _resolver(b['id'], "RECHAZADA", grupos="SUPER_ADMIN") == 200

    resp = list_autorizaciones_handler(_event(query={"estado": "APROBADA,RECHAZADA", "sucursal_id": "suc-1"}), None)
    items = {i['id']: i['estado'] for i in json.loads(resp['body'])['data']['items']}
    assert items == {a['id']: "APROBADA", b['id']: "RECHAZADA"}

    pendientes = list_autorizaciones_handler(_event(), None)
    assert json.loads(pendientes['body'])['data']['items'] == []


def test_list_estado_desconocido_es_400(mock_db):
    assert list_autorizaciones_handler(_event(query={"estado": "BORRADA"}), None)['statusCode'] == 400
