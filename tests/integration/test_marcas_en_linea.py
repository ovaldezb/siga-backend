"""Alta en línea de marcas desde el formulario de producto
(configuracion_manager.upsert_marcas_productos_handler, accion=add)."""

import json

from src.handlers.admin import configuracion_manager as cm

TENANT = 'tester'


def _ev(body, grupos='ADMIN'):
    return {
        'requestContext': {'authorizer': {'claims': {
            'custom:tenant_id': TENANT, 'cognito:groups': grupos,
        }}},
        'body': json.dumps(body),
    }


def _marcas(mock_db):
    cfg = mock_db[f't_{TENANT}']['configuracion'].find_one({'tenant_id': TENANT}) or {}
    return cfg.get('marcas') or []


def test_asesor_puede_dar_de_alta_una_marca(mock_db):
    resp = cm.upsert_marcas_productos_handler(
        _ev({'accion': 'add', 'marca': {'nombre': 'Wagner'}}, grupos='ASESOR'), None)
    assert resp['statusCode'] == 200
    assert [m['nombre'] for m in _marcas(mock_db)] == ['Wagner']


def test_asesor_no_puede_editar_ni_reemplazar(mock_db):
    assert cm.upsert_marcas_productos_handler(
        _ev({'marcas': [{'nombre': 'X'}]}, grupos='ASESOR'), None)['statusCode'] == 403
    assert cm.upsert_marcas_productos_handler(
        _ev({'accion': 'delete', 'marca': {'id': 'x'}}, grupos='ASESOR'), None)['statusCode'] == 403
    assert cm.upsert_marcas_productos_handler(
        _ev({'accion': 'add', 'marca': {'nombre': 'X'}}, grupos='MECANICO'), None)['statusCode'] == 403


def test_semilla_conserva_las_marcas_por_defecto_en_tenant_nuevo(mock_db):
    semilla = [{'id': 'bosch', 'nombre': 'Bosch', 'activa': True},
               {'id': 'ngk', 'nombre': 'NGK', 'activa': True}]
    resp = cm.upsert_marcas_productos_handler(
        _ev({'accion': 'add', 'marca': {'nombre': 'Wagner'}, 'semilla': semilla}), None)
    assert resp['statusCode'] == 200
    assert [m['nombre'] for m in _marcas(mock_db)] == ['Bosch', 'NGK', 'Wagner']

    # Con catálogo ya guardado la semilla se ignora.
    cm.upsert_marcas_productos_handler(
        _ev({'accion': 'add', 'marca': {'nombre': 'Moog'}, 'semilla': [{'nombre': 'Otra'}]}), None)
    assert [m['nombre'] for m in _marcas(mock_db)] == ['Bosch', 'NGK', 'Wagner', 'Moog']


def test_duplicada_devuelve_la_existente(mock_db):
    cm.upsert_marcas_productos_handler(_ev({'accion': 'add', 'marca': {'nombre': 'Wagner'}}), None)
    resp = cm.upsert_marcas_productos_handler(_ev({'accion': 'add', 'marca': {'nombre': 'wagner'}}), None)
    assert resp['statusCode'] == 409
    assert json.loads(resp['body'])['data']['existente']['nombre'] == 'Wagner'


def test_alta_de_marca_inactiva_la_reactiva(mock_db):
    cm.upsert_marcas_productos_handler(
        _ev({'marcas': [{'id': 'wagner', 'nombre': 'Wagner', 'activa': False}]}), None)
    resp = cm.upsert_marcas_productos_handler(
        _ev({'accion': 'add', 'marca': {'nombre': 'Wagner'}}, grupos='ASESOR'), None)
    assert resp['statusCode'] == 200
    assert _marcas(mock_db) == [{'id': 'wagner', 'nombre': 'Wagner', 'activa': True}]
