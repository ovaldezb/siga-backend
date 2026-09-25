"""Tablero del taller: GET /ordenes?vista=tablero.

Lo que se fija aquí:
- Sólo salen las OS que siguen en el taller (ENTREGADO y CANCELADO no son columnas).
- El tiempo en el estado se mide desde la última entrada de la bitácora con ese estado.
- El tiempo típico por estado es la mediana de las OS cerradas recientes.
- Al mecánico el tablero le llega sin importes.
"""
import json
from datetime import datetime, timedelta

from src.handlers.ordenes.ordenes_manager import list_ordenes_handler

TENANT = "tenant-tablero"


def _db(mock_db):
    return mock_db[f"t_{TENANT.replace('-', '')}"]


def _tablero(extra_params=None, grupo=None):
    claims = {"custom:tenant_id": TENANT}
    if grupo:
        claims["cognito:groups"] = grupo
    resp = list_ordenes_handler({
        "queryStringParameters": {"vista": "tablero", **(extra_params or {})},
        "requestContext": {"authorizer": {"claims": claims}},
    }, None)
    assert resp["statusCode"] == 200, resp["body"]
    return json.loads(resp["body"])["data"]


def _iso(dt):
    return dt.isoformat() + "Z"


def test_solo_salen_las_os_que_siguen_en_el_taller(mock_db):
    base = {"createdAt": datetime.utcnow()}
    _db(mock_db)["ordenes_servicio"].insert_many([
        {**base, "folio": f"OS-{estado}", "estado": estado}
        for estado in ["RECEPCION", "COTIZADO", "APROBADO", "EN_PROCESO",
                       "FINALIZADO", "ENTREGADO", "CANCELADO"]
    ])
    folios = {o["folio"] for o in _tablero()["items"]}
    assert folios == {"OS-RECEPCION", "OS-COTIZADO", "OS-APROBADO", "OS-EN_PROCESO", "OS-FINALIZADO"}


def test_tiempo_en_estado_sale_de_la_ultima_entrada_de_la_bitacora(mock_db):
    ahora = datetime.utcnow()
    _db(mock_db)["ordenes_servicio"].insert_one({
        "folio": "OS-1", "estado": "EN_PROCESO", "createdAt": ahora - timedelta(days=3),
        "bitacora_estados": [
            {"estado": "RECEPCION", "fecha": _iso(ahora - timedelta(days=3))},
            {"estado": "EN_PROCESO", "fecha": _iso(ahora - timedelta(hours=30))},
            {"estado": "APROBADO", "fecha": _iso(ahora - timedelta(hours=20))},
            # Volvió a EN_PROCESO: cuenta desde aquí, no desde la primera vez.
            {"estado": "EN_PROCESO", "fecha": _iso(ahora - timedelta(hours=5))},
        ],
    })
    orden = _tablero()["items"][0]
    assert 4.9 <= orden["horas_en_estado"] <= 5.1
    assert orden["dias_en_taller"] == 3
    assert "bitacora_estados" not in orden


def test_os_sin_bitacora_cuenta_desde_su_ingreso(mock_db):
    _db(mock_db)["ordenes_servicio"].insert_one({
        "folio": "OS-VIEJA", "estado": "RECEPCION",
        "createdAt": datetime.utcnow() - timedelta(hours=10),
    })
    assert 9.9 <= _tablero()["items"][0]["horas_en_estado"] <= 10.1


def test_atrasada_si_ya_paso_la_fecha_prometida(mock_db):
    ahora = datetime.utcnow()
    _db(mock_db)["ordenes_servicio"].insert_many([
        {"folio": "TARDE", "estado": "EN_PROCESO", "createdAt": ahora,
         "fechaEstimadaEntrega": _iso(ahora - timedelta(days=1))},
        {"folio": "A-TIEMPO", "estado": "EN_PROCESO", "createdAt": ahora,
         "fechaEstimadaEntrega": _iso(ahora + timedelta(days=1))},
        # Ya está lista: que el cliente no la recoja no es atraso del taller.
        {"folio": "LISTA", "estado": "FINALIZADO", "createdAt": ahora,
         "fechaEstimadaEntrega": _iso(ahora - timedelta(days=1))},
    ])
    atrasadas = {o["folio"] for o in _tablero()["items"] if o["atrasada"]}
    assert atrasadas == {"TARDE"}


def test_horas_tipicas_es_la_mediana_de_las_os_cerradas(mock_db):
    ahora = datetime.utcnow()
    inicio = ahora - timedelta(days=10)

    def cerrada(horas_en_proceso):
        fin_proceso = inicio + timedelta(hours=horas_en_proceso)
        return {
            "estado": "ENTREGADO", "updatedAt": ahora, "createdAt": inicio,
            "bitacora_estados": [
                {"estado": "EN_PROCESO", "fecha": _iso(inicio)},
                {"estado": "FINALIZADO", "fecha": _iso(fin_proceso)},
                {"estado": "ENTREGADO", "fecha": _iso(fin_proceso + timedelta(hours=2))},
            ],
        }

    # El auto olvidado (500 h) no mueve la mediana.
    _db(mock_db)["ordenes_servicio"].insert_many([cerrada(h) for h in (4, 6, 500)])
    tipicas = _tablero()["horas_tipicas"]
    assert tipicas["EN_PROCESO"] == {"horas": 6.0, "muestras": 3}
    assert tipicas["FINALIZADO"] == {"horas": 2.0, "muestras": 3}
    assert "RECEPCION" not in tipicas


def test_historico_ignora_os_cerradas_hace_mas_de_90_dias(mock_db):
    viejo = datetime.utcnow() - timedelta(days=200)
    _db(mock_db)["ordenes_servicio"].insert_one({
        "estado": "ENTREGADO", "updatedAt": viejo, "createdAt": viejo,
        "bitacora_estados": [
            {"estado": "EN_PROCESO", "fecha": _iso(viejo)},
            {"estado": "FINALIZADO", "fecha": _iso(viejo + timedelta(hours=3))},
        ],
    })
    assert _tablero()["horas_tipicas"] == {}


def test_filtra_por_sucursal(mock_db):
    base = {"createdAt": datetime.utcnow(), "estado": "RECEPCION"}
    _db(mock_db)["ordenes_servicio"].insert_many([
        {**base, "folio": "NORTE", "sucursal_id": "s-norte"},
        {**base, "folio": "SUR", "sucursal_id": "s-sur"},
    ])
    assert [o["folio"] for o in _tablero({"sucursal_id": "s-sur"})["items"]] == ["SUR"]


def test_mecanico_ve_el_tablero_sin_importes(mock_db):
    _db(mock_db)["ordenes_servicio"].insert_one({
        "folio": "OS-1", "estado": "EN_PROCESO", "createdAt": datetime.utcnow(),
        "total": 3500.0, "saldo_pendiente": 3500.0,
    })
    admin = _tablero()["items"][0]
    assert admin["total"] == 3500.0

    mecanico = _tablero(grupo=["MECANICO"])["items"][0]
    assert "total" not in mecanico
    assert "saldo_pendiente" not in mecanico
