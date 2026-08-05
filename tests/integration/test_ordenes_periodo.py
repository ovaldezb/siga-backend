"""Filtro de órdenes de servicio por periodo (año / mes) y años disponibles."""
import json
from datetime import datetime

from src.handlers.ordenes.ordenes_manager import (
    list_ordenes_handler,
    _periodo_condition,
    _anios_con_ordenes,
)

TENANT = "tenant-periodo"


def _db(mock_db):
    return mock_db[f"t_{TENANT.replace('-', '')}"]


def _event(**query):
    return {
        "queryStringParameters": {k: str(v) for k, v in query.items()},
        "requestContext": {"authorizer": {"claims": {"custom:tenant_id": TENANT}}},
    }


def _sembrar(mock_db):
    """OS repartidas en varios meses, mezclando createdAt datetime y string ISO."""
    _db(mock_db)["ordenes_servicio"].insert_many([
        {"folio": "OS-1", "estado": "ENTREGADO", "createdAt": datetime(2026, 8, 3, 10, 0)},
        {"folio": "OS-2", "estado": "ENTREGADO", "createdAt": datetime(2026, 8, 20, 9, 0)},
        {"folio": "OS-3", "estado": "ENTREGADO", "createdAt": datetime(2026, 7, 15, 9, 0)},
        # Dato migrado: la fecha quedó como string ISO, no como datetime.
        {"folio": "OS-4", "estado": "ENTREGADO", "createdAt": "2026-07-28T12:00:00Z"},
        {"folio": "OS-5", "estado": "ENTREGADO", "createdAt": datetime(2025, 12, 31, 23, 0)},
    ])


def _folios(response):
    return {o['folio'] for o in json.loads(response['body'])['data']['items']}


def test_filtra_por_anio(mock_db):
    _sembrar(mock_db)
    resp = list_ordenes_handler(_event(anio=2026), None)

    assert _folios(resp) == {"OS-1", "OS-2", "OS-3", "OS-4"}
    assert json.loads(resp['body'])['data']['total'] == 4


def test_filtra_por_mes_incluyendo_fechas_en_texto(mock_db):
    _sembrar(mock_db)
    resp = list_ordenes_handler(_event(anio=2026, mes=7), None)

    assert _folios(resp) == {"OS-3", "OS-4"}


def test_diciembre_no_desborda_al_anio_siguiente(mock_db):
    _sembrar(mock_db)
    resp = list_ordenes_handler(_event(anio=2025, mes=12), None)

    assert _folios(resp) == {"OS-5"}


def test_mes_sin_anio_se_ignora(mock_db):
    """`mes` sólo tiene sentido dentro de un año: sin `anio` no se filtra nada."""
    _sembrar(mock_db)
    resp = list_ordenes_handler(_event(mes=8), None)

    assert json.loads(resp['body'])['data']['total'] == 5


def test_periodo_invalido_es_400(mock_db):
    assert list_ordenes_handler(_event(anio="dosmil"), None)['statusCode'] == 400
    assert list_ordenes_handler(_event(anio=2026, mes=13), None)['statusCode'] == 400


def test_paginacion_respeta_el_periodo(mock_db):
    _sembrar(mock_db)
    data = json.loads(list_ordenes_handler(_event(anio=2026, limit=2, page=1), None)['body'])['data']

    assert data['total'] == 4
    assert data['totalPages'] == 2
    assert len(data['items']) == 2


def test_anios_disponibles_solo_si_se_piden(mock_db):
    _sembrar(mock_db)

    sin_pedir = json.loads(list_ordenes_handler(_event(), None)['body'])['data']
    assert 'anios' not in sin_pedir

    con_anios = json.loads(list_ordenes_handler(_event(incluir_anios=1), None)['body'])['data']
    assert con_anios['anios'] == [2026, 2025]


def test_anios_ignora_fechas_ilegibles(mock_db):
    _db(mock_db)["ordenes_servicio"].insert_many([
        {"folio": "OS-A", "createdAt": datetime(2026, 1, 5)},
        {"folio": "OS-B", "createdAt": None},
        {"folio": "OS-C"},
        {"folio": "OS-D", "createdAt": "sin-fecha"},
    ])

    assert _anios_con_ordenes(_db(mock_db)) == [2026]


def test_condicion_de_periodo_cubre_ambos_formatos():
    cond, err = _periodo_condition("2026", "2")

    assert err is None
    rangos = cond['$or']
    assert rangos[0]['createdAt'] == {'$gte': datetime(2026, 2, 1), '$lt': datetime(2026, 3, 1)}
    assert rangos[1]['createdAt'] == {'$gte': '2026-02-01T00:00:00', '$lt': '2026-03-01T00:00:00'}
