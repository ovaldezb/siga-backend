"""Un usuario del grupo MECANICO no ve ni mueve dinero.

La regla del taller: el mecánico trabaja la orden (diagnóstico, kilometraje,
testigos, evidencias) pero no ve importes ni puede tocar nada que cueste.

Se verifica en el servidor a propósito: esconder los precios sólo en pantalla es
cosmético, porque el dato igual viaja al dispositivo y se lee desde la consola
del navegador.
"""
import json
from datetime import datetime

from bson import ObjectId

from src.handlers.ordenes.ordenes_manager import (
    get_orden_handler,
    list_ordenes_handler,
    update_orden_handler,
)
from src.handlers.ventas import ventas_manager
from src.handlers.admin import reportes_manager
from src.shared.utils.auth_utils import es_mecanico

TENANT = "tenant-mecanico"


def _db(mock_db):
    return mock_db[f"t_{TENANT.replace('-', '')}"]


def _event(grupos, **extra):
    claims = {"custom:tenant_id": TENANT, "email": "juan@taller.com"}
    if grupos is not None:
        claims["cognito:groups"] = grupos
    ev = {"requestContext": {"authorizer": {"claims": claims}}}
    ev.update(extra)
    return ev


def _sembrar_orden(mock_db, **extra):
    doc = {
        "folio": "OS-500",
        "estado": "EN_PROCESO",
        "total": 4800.0,
        "subtotal": 4800.0,
        "iva": 0.0,
        "anticipo": 1000.0,
        "costo_revision": 350.0,
        "saldo_pendiente": 3800.0,
        "pago_info": {"metodo": "efectivo", "monto": 1000.0},
        "kilometraje": 90000,
        "diagnostico": "Ruido en suspensión delantera",
        "vehiculo_id": "no-es-objectid",
        "puntosArreglar": [{
            "nombre": "Suspensión",
            "items": [
                {"nombre": "Amortiguadores", "noParte": "AM-9", "piezas": 2,
                 "precioVenta": 1800.0, "subtotal": 3600.0, "precioCompra": 1100.0,
                 "costo_proveedor": 1050.0, "aprobado": True, "decision": "aprobado",
                 "linea_id": "l1"},
                {"nombre": "Bujes", "piezas": 4, "precioVenta": 300.0,
                 "subtotal": 1200.0, "rechazado": True, "decision": "rechazado"},
            ],
        }],
        "inventario": [{"nombre": "Aceite", "piezas": 4, "costo": 120.0, "subtotal": 480.0}],
        "createdAt": datetime(2026, 9, 1, 10, 0),
    }
    doc.update(extra)
    return str(_db(mock_db)["ordenes_servicio"].insert_one(doc).inserted_id)


def _importes_en(payload) -> list:
    """Cualquier llave de dinero que sobreviva en la respuesta, a cualquier nivel."""
    prohibidas = {
        "total", "subtotal", "iva", "anticipo", "costo_revision", "saldo_pendiente",
        "pago_info", "monto_credito", "precioVenta", "precio_venta", "precioCompra",
        "precio_compra", "costo", "costo_proveedor", "descuento", "importe",
    }
    encontradas = []

    def walk(node, ruta=""):
        if isinstance(node, dict):
            for k, v in node.items():
                if k in prohibidas:
                    encontradas.append(f"{ruta}.{k}")
                walk(v, f"{ruta}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{ruta}[{i}]")

    walk(payload)
    return encontradas


# ---------- el helper de rol ----------

def test_es_mecanico_solo_para_el_mecanico_puro():
    assert es_mecanico({"cognito:groups": ["MECANICO"]}) is True
    # El claim también llega como string según el authorizer.
    assert es_mecanico({"cognito:groups": "[MECANICO]"}) is True
    # Dueño que además repara: manda el rol de más alcance.
    assert es_mecanico({"cognito:groups": ["MECANICO", "ADMIN"]}) is False
    assert es_mecanico({"cognito:groups": ["ASESOR", "MECANICO"]}) is False
    assert es_mecanico({"cognito:groups": ["ASESOR"]}) is False
    assert es_mecanico({}) is False


# ---------- lectura de la orden ----------

def test_detalle_de_orden_llega_sin_un_solo_importe(mock_db):
    orden_id = _sembrar_orden(mock_db)
    ev = _event(["MECANICO"], pathParameters={"id": orden_id})

    data = json.loads(get_orden_handler(ev, None)["body"])["data"]

    assert _importes_en(data) == []
    # Y el trabajo sigue completo: nombre, número de parte, piezas y estatus.
    item = data["puntosArreglar"][0]["items"][0]
    assert item["nombre"] == "Amortiguadores"
    assert item["noParte"] == "AM-9"
    assert item["piezas"] == 2
    assert item["decision"] == "aprobado"
    assert data["diagnostico"] == "Ruido en suspensión delantera"
    assert data["kilometraje"] == 90000


def test_el_asesor_sigue_viendo_los_importes(mock_db):
    orden_id = _sembrar_orden(mock_db)
    ev = _event(["ASESOR"], pathParameters={"id": orden_id})

    data = json.loads(get_orden_handler(ev, None)["body"])["data"]

    assert data["total"] == 4800.0
    assert data["puntosArreglar"][0]["items"][0]["precioVenta"] == 1800.0


def test_listado_de_ordenes_llega_sin_importes(mock_db):
    _sembrar_orden(mock_db)
    _sembrar_orden(mock_db, folio="OS-501", estado="FINALIZADO")
    ev = _event(["MECANICO"], queryStringParameters={"limit": "50"})

    data = json.loads(list_ordenes_handler(ev, None)["body"])["data"]

    assert len(data["items"]) == 2
    assert _importes_en(data["items"]) == []
    assert {o["folio"] for o in data["items"]} == {"OS-500", "OS-501"}


# ---------- escritura en la orden ----------

def test_mecanico_guarda_su_trabajo(mock_db):
    orden_id = _sembrar_orden(mock_db)
    ev = _event(["MECANICO"], pathParameters={"id": orden_id}, body=json.dumps({
        "diagnostico": "Se cambiaron amortiguadores, falta alineación",
        "kilometraje": 90250,
        "nivel_tanque": 40,
        "testigos_encendidos": ["check_engine"],
        "estado": "FINALIZADO",
    }))

    resp = update_orden_handler(ev, None)
    assert resp["statusCode"] == 200
    assert _importes_en(json.loads(resp["body"])["data"]) == []

    guardada = _db(mock_db)["ordenes_servicio"].find_one({"_id": ObjectId(orden_id)})
    assert guardada["diagnostico"] == "Se cambiaron amortiguadores, falta alineación"
    assert guardada["kilometraje"] == 90250
    assert guardada["estado"] == "FINALIZADO"


def test_mecanico_no_puede_mover_importes(mock_db):
    """Aunque mande los campos de dinero en el body, se ignoran en silencio."""
    orden_id = _sembrar_orden(mock_db)
    ev = _event(["MECANICO"], pathParameters={"id": orden_id}, body=json.dumps({
        "diagnostico": "ok",
        "anticipo": 99999.0,
        "costo_revision": 0,
        "total": 1.0,
        "puntosArreglar": [{"nombre": "Hackeado", "items": [
            {"nombre": "Gratis", "piezas": 1, "precioVenta": 0, "subtotal": 0},
        ]}],
    }))

    assert update_orden_handler(ev, None)["statusCode"] == 200

    guardada = _db(mock_db)["ordenes_servicio"].find_one({"_id": ObjectId(orden_id)})
    assert guardada["anticipo"] == 1000.0
    assert guardada["costo_revision"] == 350.0
    # Los trabajos y sus precios quedaron intactos: el arreglo del body se ignoró.
    assert guardada["puntosArreglar"][0]["nombre"] == "Suspensión"
    assert guardada["puntosArreglar"][0]["items"][0]["precioVenta"] == 1800.0
    # Y el total se recalculó desde los items guardados, no desde el body.
    assert guardada["total"] == 3600.0
    assert guardada["diagnostico"] == "ok"


def test_asesor_si_puede_editar_los_trabajos(mock_db):
    """El recorte es sólo para el mecánico; el asesor conserva su flujo."""
    orden_id = _sembrar_orden(mock_db)
    ev = _event(["ASESOR"], pathParameters={"id": orden_id}, body=json.dumps({
        "anticipo": 2000.0,
        "puntosArreglar": [{"nombre": "Frenos", "items": [
            {"nombre": "Balatas", "piezas": 1, "precioVenta": 900, "subtotal": 900,
             "aprobado": True},
        ]}],
    }))

    assert update_orden_handler(ev, None)["statusCode"] == 200
    guardada = _db(mock_db)["ordenes_servicio"].find_one({"_id": ObjectId(orden_id)})
    assert guardada["anticipo"] == 2000.0
    assert guardada["puntosArreglar"][0]["nombre"] == "Frenos"
    assert guardada["total"] == 900.0


def test_orden_creada_por_mecanico_nace_sin_precios(mock_db):
    """Puede abrir la orden; ponerle precio es del asesor."""
    from src.handlers.ordenes.ordenes_manager import create_orden_handler

    ev = _event(["MECANICO"], body=json.dumps({
        "sucursalId": "suc-1",
        "cliente_snapshot": {"nombre": "Ana", "apellido_paterno": "Ruiz", "telefono": "5512345678"},
        "vehiculo_snapshot": {"marca": "Nissan", "modelo": "Versa", "anio": 2021, "placas": "XYZ-1"},
        "falla_reportada": "No enciende",
        "anticipo": 5000.0,
        "puntosArreglar": [{"nombre": "Eléctrico", "items": [
            {"nombre": "Batería", "piezas": 1, "precioVenta": 3200.0, "subtotal": 3200.0},
        ]}],
    }))

    resp = create_orden_handler(ev, None)
    assert resp["statusCode"] in (200, 201), resp["body"]

    guardada = _db(mock_db)["ordenes_servicio"].find_one({"falla_reportada": "No enciende"})
    assert guardada["total"] == 0
    assert guardada.get("anticipo", 0) == 0
    item = guardada["puntosArreglar"][0]["items"][0]
    assert item["nombre"] == "Batería"       # el trabajo sí queda registrado
    assert "precioVenta" not in item


# ---------- módulos de dinero ----------

def test_endpoints_de_cobro_rechazan_al_mecanico(mock_db):
    ev = _event(["MECANICO"], body=json.dumps({}), pathParameters={"id": str(ObjectId())},
                queryStringParameters={})

    for handler in (
        ventas_manager.create_venta_handler,
        ventas_manager.registrar_abono_handler,
        ventas_manager.update_metodo_pago_handler,
        ventas_manager.list_cxc_handler,
        ventas_manager.list_ventas_handler,
        ventas_manager.get_venta_by_id_handler,
        reportes_manager.get_kpis_handler,
        reportes_manager.get_customer_history_handler,
    ):
        resp = handler(ev, None)
        assert resp["statusCode"] == 403, handler.__name__
        assert "cobros" in json.loads(resp["body"])["message"]


def test_esos_mismos_endpoints_siguen_abiertos_para_asesor(mock_db):
    ev = _event(["ASESOR"], queryStringParameters={})
    resp = ventas_manager.list_cxc_handler(ev, None)
    assert resp["statusCode"] == 200
