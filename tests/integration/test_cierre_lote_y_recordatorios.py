"""Cierre en bloque de órdenes en FINALIZADO y seguimiento de recordatorios de servicio.

El taller cobra y no registra el cobro: las órdenes se quedan en FINALIZADO. El
cierre en bloque las cierra por el mismo camino que caja (abono o venta), y una
orden que no se puede cerrar no detiene a las demás.
"""
import json

from bson import ObjectId

from src.handlers.ordenes.cierre_lote_manager import cerrar_ordenes_lote_handler
from src.handlers.ventas.ventas_manager import create_venta_handler
from src.handlers.vehiculos.recordatorios_manager import registrar_seguimiento_handler

TENANT = "tallerlote"
SUCURSAL = "suc-a"


def _claims(grupo="ADMIN"):
    return {"custom:tenant_id": TENANT, "sub": "u-1", "email": "admin@taller.com", "name": "Admin",
            "cognito:groups": grupo}


def _ev(body, path=None, grupo="ADMIN"):
    e = {"body": json.dumps(body), "requestContext": {"authorizer": {"claims": _claims(grupo)}}}
    if path:
        e["pathParameters"] = path
    return e


def _orden(db, folio, *, items=None, estado="FINALIZADO"):
    items = items if items is not None else [{
        "linea_id": f"{folio}-1", "item_id": "manual", "nombre": "Servicio mayor", "piezas": 1,
        "precioVenta": 1200.0, "precioCompra": 0, "tipo": "SERVICIO",
    }, {
        "linea_id": f"{folio}-2", "item_id": "manual", "nombre": "Filtro de aceite", "piezas": 2,
        "precioVenta": 150.0, "precioCompra": 80.0, "tipo": "PRODUCTO",
    }, {
        "linea_id": f"{folio}-3", "item_id": "manual", "nombre": "Rechazado", "piezas": 1,
        "precioVenta": 999.0, "rechazado": True,
    }]
    return str(db["ordenes_servicio"].insert_one({
        "tenant_id": TENANT, "sucursal_id": SUCURSAL, "folio": folio, "estado": estado,
        "cliente_snapshot": {"id": "cli-1", "nombre": "Ana", "apellido_paterno": "López"},
        "puntosArreglar": [{"nombre": "Servicio", "items": items}],
    }).inserted_id)


def _data(resp):
    return json.loads(resp["body"])["data"]


def test_cobra_en_bloque_las_que_no_pasaron_por_caja(mock_db):
    db = mock_db[f"t_{TENANT}"]
    os_ok = _orden(db, "OS-1")
    os_sin_precio = _orden(db, "OS-2", items=[{
        "item_id": "manual", "nombre": "Mano de obra", "piezas": 1, "precioVenta": 0, "tipo": "SERVICIO"}])
    os_en_proceso = _orden(db, "OS-3", estado="EN_PROCESO")

    resp = cerrar_ordenes_lote_handler(_ev({
        "orden_ids": [os_ok, os_sin_precio, os_en_proceso, "no-es-id"], "metodo": "EFECTIVO"}), None)
    assert resp["statusCode"] == 200, resp["body"]
    data = _data(resp)
    assert data["cerradas"] == 1 and data["fallidas"] == 3
    por_folio = {r["folio"]: r for r in data["resultados"]}
    assert por_folio["OS-1"]["accion"] == "COBRADA" and por_folio["OS-1"]["monto"] == 1500
    assert "no tiene precio" in por_folio["OS-2"]["mensaje"]
    assert "EN_PROCESO" in por_folio["OS-3"]["mensaje"]

    orden = db["ordenes_servicio"].find_one({"_id": ObjectId(os_ok)})
    assert orden["estado"] == "ENTREGADO" and orden["venta_id"]
    venta = db["ventas"].find_one({"orden_id": os_ok})
    assert venta["total"] == 1500 and venta["saldo_pendiente"] == 0
    assert len(venta["items"]) == 2  # el rechazado no se cobra
    assert db["ordenes_servicio"].find_one({"_id": ObjectId(os_sin_precio)})["estado"] == "FINALIZADO"


def test_liquida_en_bloque_los_creditos(mock_db):
    db = mock_db[f"t_{TENANT}"]
    db["clientes"].insert_one({"_id": ObjectId("6a6b632b0f5f78c2a8647073"), "nombre": "Agro", "limite_credito": 50000})
    orden_id = _orden(db, "OS-9")
    resp = create_venta_handler(_ev({
        "cliente_id": "6a6b632b0f5f78c2a8647073", "cliente_nombre": "Agro", "sucursal_id": SUCURSAL,
        "orden_id": orden_id, "metodo_pago": "CREDITO",
        "pagos": [{"metodo": "CREDITO", "monto": 1500, "forma_pago_sat": "99"}],
        "items": [{"producto": {"id": "manual", "nombre": "Servicio", "tipo": "SERVICIO"},
                   "cantidad": 1, "precio_unitario": 1500}],
    }), None)
    assert resp["statusCode"] == 201, resp["body"]
    assert db["ordenes_servicio"].find_one({"_id": ObjectId(orden_id)})["estado"] == "FINALIZADO"

    data = _data(cerrar_ordenes_lote_handler(_ev({"orden_ids": [orden_id], "metodo": "TRANSFERENCIA"}), None))
    assert data["cerradas"] == 1 and data["resultados"][0]["accion"] == "LIQUIDADA"
    orden = db["ordenes_servicio"].find_one({"_id": ObjectId(orden_id)})
    assert orden["estado"] == "ENTREGADO" and orden["saldo_pendiente"] == 0
    assert db["ventas"].count_documents({"orden_id": orden_id}) == 1  # no se crea otra venta


def test_cierre_en_bloque_valida(mock_db):
    assert cerrar_ordenes_lote_handler(_ev({"orden_ids": ["x"], "metodo": "EFECTIVO"}, grupo="MECANICO"), None)["statusCode"] == 403
    assert cerrar_ordenes_lote_handler(_ev({"orden_ids": [], "metodo": "EFECTIVO"}), None)["statusCode"] == 400
    assert cerrar_ordenes_lote_handler(_ev({"orden_ids": ["x"]}), None)["statusCode"] == 400
    # Un cierre "a crédito" no cierra nada: tiene que ser con lo que pagó el cliente.
    assert cerrar_ordenes_lote_handler(_ev({"orden_ids": ["x"], "metodo": "CREDITO"}), None)["statusCode"] == 400
    assert cerrar_ordenes_lote_handler(_ev({"orden_ids": [str(i) for i in range(21)], "metodo": "EFECTIVO"}), None)["statusCode"] == 400


def test_seguimiento_de_recordatorios(mock_db):
    db = mock_db[f"t_{TENANT}"]
    vid = str(db["vehiculos"].insert_one({"placas": "ABC-123"}).inserted_id)

    resp = registrar_seguimiento_handler(_ev({"accion": "pospuesto", "dias": 45, "nota": "Viaja"}, {"id": vid}), None)
    assert resp["statusCode"] == 200, resp["body"]
    v = db["vehiculos"].find_one({"_id": ObjectId(vid)})
    assert v["recordatorio_servicio"]["estado"] == "POSPUESTO" and v["recordatorio_servicio"]["nota"] == "Viaja"
    assert (v["recordatorio_servicio"]["hasta"] - v["recordatorio_servicio"]["fecha"]).days == 45

    registrar_seguimiento_handler(_ev({"accion": "DESCARTADO"}, {"id": vid}), None)
    assert db["vehiculos"].find_one({"_id": ObjectId(vid)})["recordatorio_servicio"]["hasta"] is None

    registrar_seguimiento_handler(_ev({"accion": "REACTIVAR"}, {"id": vid}), None)
    v = db["vehiculos"].find_one({"_id": ObjectId(vid)})
    assert "recordatorio_servicio" not in v and len(v["recordatorios_historial"]) == 3

    assert registrar_seguimiento_handler(_ev({"accion": "OTRA"}, {"id": vid}), None)["statusCode"] == 400
    assert registrar_seguimiento_handler(_ev({"accion": "POSPUESTO", "dias": 0}, {"id": vid}), None)["statusCode"] == 400
    assert registrar_seguimiento_handler(_ev({"accion": "CONTACTADO"}, {"id": str(ObjectId())}), None)["statusCode"] == 404
    assert registrar_seguimiento_handler(_ev({"accion": "CONTACTADO"}, {"id": vid}, grupo="MECANICO"), None)["statusCode"] == 403
