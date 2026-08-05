"""Límite de 2 sesiones concurrentes por usuario."""
import json
from datetime import datetime, timedelta

from src.handlers.auth import sesiones_manager
from src.handlers.auth.sesiones_manager import (
    registrar_sesion_handler,
    estado_sesion_handler,
    cerrar_sesion_handler,
    MAX_SESIONES,
)

USER_SUB = "sub-abc-123"


def _event(device_id=None, en_body=True, sub=USER_SUB):
    event = {
        "requestContext": {"authorizer": {"claims": {
            "sub": sub,
            "email": "asesor@taller.com",
            "custom:tenant_id": "tenant-1",
        }}}
    }
    if device_id and en_body:
        event["body"] = json.dumps({"device_id": device_id})
    elif device_id:
        event["queryStringParameters"] = {"device_id": device_id}
    return event


def _sesiones(mock_db):
    return mock_db["_platform"]["sesiones_usuario"]


def _registrar(device_id, sub=USER_SUB):
    return registrar_sesion_handler(_event(device_id, sub=sub), None)


def test_dos_sesiones_conviven(mock_db):
    assert _registrar("dev-1")['statusCode'] == 200
    resp = _registrar("dev-2")

    body = json.loads(resp['body'])
    assert body['data']['sesiones_activas'] == 2
    assert body['data']['sesiones_cerradas'] == 0
    assert _sesiones(mock_db).count_documents({"revocada_en": None}) == 2


def test_tercera_sesion_revoca_la_mas_antigua(mock_db):
    _registrar("dev-1")
    _registrar("dev-2")

    # dev-1 es la más antigua: se le envejece el latido para que el orden sea inequívoco.
    _sesiones(mock_db).update_one(
        {"device_id": "dev-1"},
        {"$set": {"ultimo_acceso": datetime.utcnow() - timedelta(minutes=30)}},
    )

    body = json.loads(_registrar("dev-3")['body'])
    assert body['data']['sesiones_cerradas'] == 1
    assert body['data']['sesiones_activas'] == MAX_SESIONES

    revocada = _sesiones(mock_db).find_one({"device_id": "dev-1"})
    assert revocada['revocada_en'] is not None
    assert revocada['motivo'] == 'limite_sesiones'
    assert _sesiones(mock_db).find_one({"device_id": "dev-2"})['revocada_en'] is None


def test_dispositivo_revocado_deja_de_estar_vigente(mock_db):
    _registrar("dev-1")
    _registrar("dev-2")
    # Los tres registros caen en el mismo instante dentro del test; se envejece
    # dev-1 para que sea inequívocamente el más antiguo y el aserto no dependa
    # de la resolución del reloj.
    _sesiones(mock_db).update_one(
        {"device_id": "dev-1"},
        {"$set": {"ultimo_acceso": datetime.utcnow() - timedelta(minutes=30)}},
    )
    _registrar("dev-3")

    body = json.loads(estado_sesion_handler(_event("dev-1", en_body=False), None)['body'])
    assert body['data']['vigente'] is False
    assert body['data']['motivo'] == 'limite_sesiones'

    body_vivo = json.loads(estado_sesion_handler(_event("dev-3", en_body=False), None)['body'])
    assert body_vivo['data']['vigente'] is True


def test_reentrar_desde_el_mismo_dispositivo_no_consume_cupo(mock_db):
    _registrar("dev-1")
    _registrar("dev-1")
    _registrar("dev-2")

    assert _sesiones(mock_db).count_documents({"revocada_en": None}) == 2


def test_sesion_inactiva_no_bloquea_el_cupo(mock_db):
    """Un navegador cerrado sin logout no debe quitarle su lugar a nadie."""
    _registrar("dev-1")
    _registrar("dev-2")
    _sesiones(mock_db).update_one(
        {"device_id": "dev-1"},
        {"$set": {"ultimo_acceso": datetime.utcnow() - timedelta(
            hours=sesiones_manager.VENTANA_INACTIVIDAD_HORAS + 1)}},
    )

    body = json.loads(_registrar("dev-3")['body'])
    # dev-1 ya no cuenta como viva, así que dev-2 sobrevive sin revocaciones.
    assert body['data']['sesiones_cerradas'] == 0
    assert _sesiones(mock_db).find_one({"device_id": "dev-2"})['revocada_en'] is None


def test_dispositivo_desconocido_se_da_de_alta_en_el_latido(mock_db):
    """Sesiones abiertas antes de existir el control no se cierran por el deploy."""
    body = json.loads(estado_sesion_handler(_event("dev-viejo", en_body=False), None)['body'])

    assert body['data']['vigente'] is True
    assert _sesiones(mock_db).find_one({"device_id": "dev-viejo"}) is not None


def test_cerrar_sesion_libera_el_cupo(mock_db):
    _registrar("dev-1")
    _registrar("dev-2")

    assert cerrar_sesion_handler(_event("dev-1", en_body=False), None)['statusCode'] == 200
    assert _sesiones(mock_db).find_one({"device_id": "dev-1"}) is None

    body = json.loads(_registrar("dev-3")['body'])
    assert body['data']['sesiones_cerradas'] == 0


def test_el_limite_es_por_usuario(mock_db):
    _registrar("dev-1")
    _registrar("dev-2")
    _registrar("otro-1", sub="sub-otro-999")

    assert json.loads(
        _registrar("otro-2", sub="sub-otro-999")['body']
    )['data']['sesiones_cerradas'] == 0
    assert _sesiones(mock_db).count_documents({"user_sub": USER_SUB, "revocada_en": None}) == 2


def test_device_id_obligatorio(mock_db):
    assert registrar_sesion_handler(_event(), None)['statusCode'] == 400
    assert estado_sesion_handler(_event(), None)['statusCode'] == 400
