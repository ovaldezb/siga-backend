import json
from datetime import datetime, timedelta

from src.handlers.ordenes.ordenes_manager import list_sugerencias_pendientes_handler

TENANT = "taller_test"
VEHICULO = "veh-1"


def _event(**qs):
    return {
        "queryStringParameters": {"vehiculo_id": VEHICULO, **qs},
        "requestContext": {"authorizer": {"claims": {"custom:tenant_id": TENANT}}},
    }


def _orden(db, folio, dias_atras, items, estado="ENTREGADO"):
    db["ordenes_servicio"].insert_one({
        "tenant_id": TENANT,
        "folio": folio,
        "estado": estado,
        "vehiculo_id": VEHICULO,
        "createdAt": datetime(2026, 1, 1) + timedelta(days=-dias_atras),
        "puntosArreglar": [{"nombre": "Suspensión", "items": items}],
    })


def _item(nombre, rechazado):
    return {
        "nombre": nombre,
        "piezas": 2,
        "precioVenta": 1200,
        "precioCompra": 700,
        "subtotal": 2400,
        "aprobado": not rechazado,
        "rechazado": rechazado,
        "entregado": False,
    }


def _sugerencias(response):
    assert response["statusCode"] == 200
    return json.loads(response["body"])["data"]["items"]


def test_item_rechazado_se_sugiere(mock_db):
    """Lo que el cliente rechazó en una visita se re-ofrece en la siguiente."""
    db = mock_db[f"t_{TENANT}"]
    _orden(db, "OS-1", 30, [_item("Horquillas", rechazado=True)])

    sugerencias = _sugerencias(list_sugerencias_pendientes_handler(_event(), None))

    assert len(sugerencias) == 1
    assert sugerencias[0]["nombre"] == "Horquillas"
    assert sugerencias[0]["folio_origen"] == "OS-1"


def test_item_aceptado_despues_deja_de_sugerirse(mock_db):
    """Reporte del taller: 1ra visita no autorizó horquillas, 2da sí y se vendieron.
    En la 3ra visita ya no deben aparecer como pendientes."""
    db = mock_db[f"t_{TENANT}"]
    _orden(db, "OS-1", 30, [_item("Horquillas", rechazado=True)])
    _orden(db, "OS-2", 15, [_item("Horquillas", rechazado=False)])

    sugerencias = _sugerencias(list_sugerencias_pendientes_handler(_event(), None))

    assert sugerencias == []


def test_aceptado_ignora_mayusculas_y_espacios(mock_db):
    """El asesor reescribe el nombre a mano; no debe reaparecer por un espacio."""
    db = mock_db[f"t_{TENANT}"]
    _orden(db, "OS-1", 30, [_item("Horquillas", rechazado=True)])
    _orden(db, "OS-2", 15, [_item("  horquillas ", rechazado=False)])

    assert _sugerencias(list_sugerencias_pendientes_handler(_event(), None)) == []


def test_aceptado_antes_del_rechazo_sigue_sugiriendo(mock_db):
    """Se le vendieron horquillas hace un año y hoy rechaza unas nuevas:
    el rechazo es posterior, así que sigue siendo una sugerencia viva."""
    db = mock_db[f"t_{TENANT}"]
    _orden(db, "OS-1", 300, [_item("Horquillas", rechazado=False)])
    _orden(db, "OS-2", 10, [_item("Horquillas", rechazado=True)])

    sugerencias = _sugerencias(list_sugerencias_pendientes_handler(_event(), None))

    assert len(sugerencias) == 1
    assert sugerencias[0]["folio_origen"] == "OS-2"


def test_orden_cancelada_no_cuenta_como_aceptacion(mock_db):
    """Una OS cancelada no vendió nada: la sugerencia sigue viva."""
    db = mock_db[f"t_{TENANT}"]
    _orden(db, "OS-1", 30, [_item("Horquillas", rechazado=True)])
    _orden(db, "OS-2", 15, [_item("Horquillas", rechazado=False)], estado="CANCELADO")

    sugerencias = _sugerencias(list_sugerencias_pendientes_handler(_event(), None))

    assert len(sugerencias) == 1
    assert sugerencias[0]["folio_origen"] == "OS-1"


def test_rechazos_repetidos_se_sugieren_una_sola_vez(mock_db):
    """Rechazado en dos visitas ⇒ una sugerencia, la del rechazo más reciente."""
    db = mock_db[f"t_{TENANT}"]
    _orden(db, "OS-1", 60, [_item("Horquillas", rechazado=True)])
    _orden(db, "OS-2", 20, [_item("Horquillas", rechazado=True)])

    sugerencias = _sugerencias(list_sugerencias_pendientes_handler(_event(), None))

    assert len(sugerencias) == 1
    assert sugerencias[0]["folio_origen"] == "OS-2"


def test_importada_en_la_orden_actual_no_se_resugiere(mock_db):
    """Con exclude_orden_id la OS actual no genera sugerencias, pero sí cuenta
    como aceptación: al reabrirla no debe re-ofrecer lo que ya tiene dentro."""
    db = mock_db[f"t_{TENANT}"]
    _orden(db, "OS-1", 30, [_item("Horquillas", rechazado=True)])
    _orden(db, "OS-2", 1, [_item("Horquillas", rechazado=False)], estado="RECEPCION")
    actual = db["ordenes_servicio"].find_one({"folio": "OS-2"})

    sugerencias = _sugerencias(
        list_sugerencias_pendientes_handler(_event(exclude_orden_id=str(actual["_id"])), None)
    )

    assert sugerencias == []


def _item_pendiente(nombre):
    """Item traído por una plantilla de revisión: ni aprobado ni rechazado todavía."""
    return {**_item(nombre, rechazado=False), "aprobado": False}


def test_aprobado_en_la_misma_orden_no_oculta_el_rechazo(mock_db):
    """Reporte 2026-10-19 (OS-0417): "MANO DE OBRA" aprobada en la afinación ocultaba la
    "MANO DE OBRA" de suspensión que el cliente rechazó en la misma OS."""
    db = mock_db[f"t_{TENANT}"]
    db["ordenes_servicio"].insert_one({
        "tenant_id": TENANT, "folio": "OS-1", "estado": "ENTREGADO", "vehiculo_id": VEHICULO,
        "createdAt": datetime(2026, 1, 1),
        "puntosArreglar": [
            {"nombre": "Afinación", "items": [_item("MANO DE OBRA", rechazado=False)]},
            {"nombre": "Suspensión", "items": [_item("MANO DE OBRA", rechazado=True)]},
        ],
    })

    sugerencias = _sugerencias(list_sugerencias_pendientes_handler(_event(), None))

    assert [(s["punto_nombre"], s["nombre"]) for s in sugerencias] == [("Suspensión", "MANO DE OBRA")]


def test_item_pendiente_en_orden_posterior_no_cuenta_como_aceptado(mock_db):
    """La OS nueva trae de nuevo "Horquillas" sin autorizar: el rechazo anterior sigue vivo."""
    db = mock_db[f"t_{TENANT}"]
    _orden(db, "OS-1", 30, [_item("Horquillas", rechazado=True)])
    _orden(db, "OS-2", 1, [_item_pendiente("Horquillas")], estado="RECEPCION")

    sugerencias = _sugerencias(list_sugerencias_pendientes_handler(_event(), None))

    assert len(sugerencias) == 1
    assert sugerencias[0]["folio_origen"] == "OS-1"
