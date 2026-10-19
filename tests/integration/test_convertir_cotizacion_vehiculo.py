"""Reporte 2026-10-19: la OS-0417 se creó convirtiendo una cotización y quedó sin
`vehiculo_id`. En la siguiente visita (OS-0420) no aparecieron los rechazos ni los
próximos cambios, porque todo el historial del vehículo se busca por ese campo."""
import json
from datetime import datetime

from bson import ObjectId

from src.handlers.cotizaciones.cotizaciones_manager import convertir_a_os_handler
from src.handlers.ordenes.ordenes_manager import (
    list_sugerencias_pendientes_handler,
    update_orden_handler,
)

TENANT = "taller_test"
SUCURSAL = "suc-1"


def _claims():
    return {"custom:tenant_id": TENANT, "email": "asesor@taller.mx", "custom:role": "ADMIN"}


def _event(path=None, body=None, qs=None):
    return {
        "pathParameters": path or {},
        "queryStringParameters": qs,
        "body": json.dumps(body or {}),
        "requestContext": {"authorizer": {"claims": _claims()}},
    }


def _vehiculo(db):
    return str(db["vehiculos"].insert_one({
        "marca": "Nissan", "modelo": "Platina", "placas": "ADH-017-F", "cliente_id": "cli-1",
    }).inserted_id)


def _cotizacion(db, vehiculo_id):
    return str(db["cotizaciones"].insert_one({
        "tenant_id": TENANT, "tipo": "CLIENTE", "status": "ENVIADA", "sucursal_id": SUCURSAL,
        "cliente_snapshot": {"id": "cli-1", "nombre": "Jose Luis"},
        # Igual que en prod: el id del vehículo solo viene dentro del snapshot.
        "vehiculo_snapshot": {"id": vehiculo_id, "marca": "Nissan", "modelo": "Platina"},
        "kilometraje": 216631,
        "proximo_cambio_aceite": 221631,
        "proximo_cambio_aceite_fecha": "2027-04",
        "puntosArreglar": [{"nombre": "Dirección", "items": [{
            "nombre": "VIELETAS", "piezas": 2, "precioVenta": 396,
            "aprobado": False, "rechazado": True,
        }]}],
        "createdAt": datetime(2026, 10, 15),
    }).inserted_id)


def _convertir(db, cot_id):
    res = convertir_a_os_handler(_event(path={"id": cot_id}), None)
    assert res["statusCode"] == 201, res["body"]
    return json.loads(res["body"])["data"]["os_id"]


def test_os_convertida_queda_ligada_al_vehiculo(mock_db):
    db = mock_db[f"t_{TENANT}"]
    vid = _vehiculo(db)
    os_id = _convertir(db, _cotizacion(db, vid))

    orden = db["ordenes_servicio"].find_one({"_id": ObjectId(os_id)})
    assert orden["vehiculo_id"] == vid

    # Los próximos cambios capturados en la cotización llegan al vehículo.
    veh = db["vehiculos"].find_one({"_id": ObjectId(vid)})
    assert veh["proximo_cambio_aceite"] == 221631
    assert veh["proximo_cambio_aceite_fecha"] == "2027-04"


def test_rechazos_de_os_convertida_se_sugieren_en_la_siguiente_visita(mock_db):
    db = mock_db[f"t_{TENANT}"]
    vid = _vehiculo(db)
    _convertir(db, _cotizacion(db, vid))

    res = list_sugerencias_pendientes_handler(_event(qs={"vehiculo_id": vid}), None)

    items = json.loads(res["body"])["data"]["items"]
    assert [i["nombre"] for i in items] == ["VIELETAS"]


def test_os_legacy_sin_vehiculo_id_se_autocura_al_guardar(mock_db):
    db = mock_db[f"t_{TENANT}"]
    vid = _vehiculo(db)
    os_id = str(db["ordenes_servicio"].insert_one({
        "tenant_id": TENANT, "folio": "OS-0417", "estado": "EN_PROCESO", "sucursal_id": SUCURSAL,
        "vehiculo_snapshot": {"id": vid}, "puntosArreglar": [],
        "createdAt": datetime(2026, 10, 15),
    }).inserted_id)

    res = update_orden_handler(
        _event(path={"id": os_id}, body={"proximo_cambio_bujias": 226631}), None)
    assert res["statusCode"] == 200, res["body"]

    assert db["ordenes_servicio"].find_one({"_id": ObjectId(os_id)})["vehiculo_id"] == vid
    assert db["vehiculos"].find_one({"_id": ObjectId(vid)})["proximo_cambio_bujias"] == 226631
