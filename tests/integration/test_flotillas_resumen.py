"""Tests del resumen de flotilla (flotillas_manager.get_flotilla_handler).

El foco es el crédito: la flotilla NO tiene línea propia — la suma de sus
miembros es lo único real. Antes el UI pintaba un límite fijo de demostración y
contaba como "consumido" el pipeline de OS abiertas, así que la ficha mostraba
un crédito que el POS contradecía. Aquí se fija el criterio:

- límite  = Σ `cliente.limite_credito` de los miembros.
- usado   = Σ `ventas.saldo_pendiente` (> 0) de los miembros, igual que
            clientes_manager y ventas_manager.
- el pipeline de OS abiertas no toca el crédito.

Cubre además las unidades del parque vehicular y el conteo del listado.
"""

import json

import pytest

from src.handlers.flotillas import flotillas_manager as fm
from src.shared.utils.indexes import reset_cache


TENANT = 'tester'
COGNITO_EVENT = {
    'requestContext': {
        'authorizer': {'claims': {'custom:tenant_id': TENANT, 'email': 'asesor@taller.com'}}
    }
}


def _db(mock_db):
    return mock_db[f't_{TENANT}']


def _data(resp):
    return json.loads(resp['body'])['data']


@pytest.fixture
def flotilla(mock_db):
    """Flotilla con dos clientes: uno con crédito y deuda, otro sin línea."""
    reset_cache()
    db = _db(mock_db)
    flot_id = str(db['flotillas'].insert_one({
        'nombre': 'Transportes del Norte',
        'tenant_id': TENANT,
    }).inserted_id)

    con_credito = str(db['clientes'].insert_one({
        'nombre': 'Operaciones', 'apellido_paterno': 'Norte',
        'flotilla_id': flot_id, 'limite_credito': 100000, 'dias_credito': 30,
    }).inserted_id)
    sin_credito = str(db['clientes'].insert_one({
        'nombre': 'Logística', 'apellido_paterno': 'Sur',
        'flotilla_id': flot_id,
    }).inserted_id)
    # Cliente de otra flotilla: no debe contaminar ningún agregado.
    ajeno = str(db['clientes'].insert_one({
        'nombre': 'Ajeno', 'apellido_paterno': 'Externo',
        'limite_credito': 999999,
    }).inserted_id)

    db['vehiculos'].insert_many([
        {'placas': 'AAA-111', 'marca': 'Nissan', 'modelo': 'NP300', 'cliente_id': con_credito},
        {'placas': 'BBB-222', 'marca': 'Ford', 'modelo': 'Transit', 'cliente_id': con_credito},
        {'placas': 'CCC-333', 'marca': 'RAM', 'modelo': '700', 'cliente_id': sin_credito},
        {'placas': 'ZZZ-999', 'marca': 'Otro', 'modelo': 'Ajeno', 'cliente_id': ajeno},
    ])

    # CxC viva del miembro con crédito + una venta ya pagada (no cuenta) + una
    # venta del cliente ajeno (tampoco).
    db['ventas'].insert_many([
        {'cliente_id': con_credito, 'saldo_pendiente': 15000},
        {'cliente_id': con_credito, 'saldo_pendiente': 5000},
        {'cliente_id': con_credito, 'saldo_pendiente': 0},
        {'cliente_id': ajeno, 'saldo_pendiente': 400000},
    ])

    # OS abierta: pipeline, NO crédito.
    db['ordenes_servicio'].insert_one({
        'folio': 'OS-1', 'estado': 'EN_PROCESO', 'total': 80000,
        'cliente_snapshot': {'id': con_credito},
    })

    return flot_id, con_credito, sin_credito


def test_credito_suma_limites_de_los_miembros(mock_db, flotilla):
    flot_id, _, _ = flotilla
    ev = dict(COGNITO_EVENT, pathParameters={'id': flot_id})

    d = _data(fm.get_flotilla_handler(ev, None))

    assert d['credito_limite'] == 100000       # sólo el miembro con línea
    assert d['credito_usado'] == 20000         # 15000 + 5000, el saldo 0 no cuenta
    assert d['credito_disponible'] == 80000
    assert d['clientes_con_credito'] == 1


def test_pipeline_de_os_no_consume_credito(mock_db, flotilla):
    """La OS abierta de $80,000 aparece como pipeline, no como crédito usado."""
    flot_id, _, _ = flotilla
    ev = dict(COGNITO_EVENT, pathParameters={'id': flot_id})

    d = _data(fm.get_flotilla_handler(ev, None))

    assert d['monto_pipeline'] == 80000
    assert d['credito_usado'] == 20000


def test_saldo_y_unidades_por_cliente(mock_db, flotilla):
    flot_id, con_credito, sin_credito = flotilla
    ev = dict(COGNITO_EVENT, pathParameters={'id': flot_id})

    d = _data(fm.get_flotilla_handler(ev, None))
    por_id = {c['id']: c for c in d['clientes']}

    assert por_id[con_credito]['saldo_credito'] == 20000
    assert por_id[con_credito]['num_vehiculos'] == 2
    assert por_id[sin_credito]['saldo_credito'] == 0
    assert por_id[sin_credito]['limite_credito'] == 0
    assert por_id[sin_credito]['num_vehiculos'] == 1


def test_detalle_trae_las_unidades_con_su_dueno(mock_db, flotilla):
    flot_id, _, _ = flotilla
    ev = dict(COGNITO_EVENT, pathParameters={'id': flot_id})

    d = _data(fm.get_flotilla_handler(ev, None))

    assert d['num_vehiculos'] == 3             # el del cliente ajeno queda fuera
    assert d['vehiculos_truncados'] is False
    placas = sorted(v['placas'] for v in d['vehiculos'])
    assert placas == ['AAA-111', 'BBB-222', 'CCC-333']
    assert all(v['cliente_nombre'] for v in d['vehiculos'])


def test_flotilla_vacia_no_inventa_credito(mock_db):
    reset_cache()
    db = _db(mock_db)
    flot_id = str(db['flotillas'].insert_one({'nombre': 'Nueva', 'tenant_id': TENANT}).inserted_id)
    ev = dict(COGNITO_EVENT, pathParameters={'id': flot_id})

    d = _data(fm.get_flotilla_handler(ev, None))

    assert d['num_clientes'] == 0
    assert d['num_vehiculos'] == 0
    assert d['credito_limite'] == 0
    assert d['credito_usado'] == 0
    assert d['credito_disponible'] == 0
    assert d['vehiculos'] == []


def test_listado_cuenta_clientes_y_unidades(mock_db, flotilla):
    flot_id, _, _ = flotilla

    items = _data(fm.list_flotillas_handler(dict(COGNITO_EVENT), None))
    fila = next(f for f in items if f['id'] == flot_id)

    assert fila['num_clientes'] == 2
    assert fila['num_vehiculos'] == 3
