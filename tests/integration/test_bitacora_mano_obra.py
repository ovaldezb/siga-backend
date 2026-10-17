"""Mano de obra a precio capturado en caja: se permite, pero queda en la bitácora."""
import json
from bson import ObjectId
from src.handlers.ventas.ventas_manager import create_venta_handler
from src.handlers.admin.bitacora_manager import list_bitacora_handler, revisar_bitacora_handler

TENANT = "tallertest"
SUCURSAL = "suc-a"


def _claims(grupo=None):
    c = {"custom:tenant_id": TENANT, "sub": "cajero-1", "email": "caja@taller.com", "name": "Cajera Ana"}
    if grupo:
        c["cognito:groups"] = grupo
    return c


def _servicio(db, precio=0.0, **extra):
    return str(db["items"].insert_one({
        "nombre": "Afinación mayor", "tipo": "SERVICIO", "precio_venta": precio,
        "maneja_inventario": False, "sucursal_id": SUCURSAL, "tenant_id": TENANT, **extra,
    }).inserted_id)


def _vender(lineas, orden_id=None):
    total = sum(l["precio_unitario"] * l.get("cantidad", 1) for l in lineas)
    body = {
        "sucursal_id": SUCURSAL, "cliente_id": "PUBLICO_GENERAL", "items": lineas,
        "metodo_pago": "EFECTIVO", "pagos": [{"metodo": "EFECTIVO", "monto": total, "forma_pago_sat": "01"}],
        "forma_pago_sat": "01",
    }
    if orden_id:
        body["orden_id"] = orden_id
    resp = create_venta_handler({"body": json.dumps(body), "requestContext": {"authorizer": {"claims": _claims()}}}, None)
    assert resp["statusCode"] == 201, resp["body"]
    return json.loads(resp["body"])["data"]


def _linea(item_id, precio, **extra):
    return {"producto": {"id": item_id, "nombre": "Afinación mayor", "tipo": "SERVICIO"},
            "cantidad": 1, "precio_unitario": precio, **extra}


def test_servicio_en_cero_cobrado_con_precio_queda_en_bitacora(mock_db):
    db = mock_db[f"t_{TENANT}"]
    sid = _servicio(db, precio=0.0)

    venta = _vender([_linea(sid, 850.0)])

    assert venta["total"] == 850.0
    linea = db["ventas"].find_one({"folio": venta["folio"]})["items"][0]
    assert linea["precio_manual"] is True and linea["precio_referencia"] == 0.0

    ev = db["bitacora"].find_one({"venta_id": venta["id"]})
    assert ev["tipo"] == "PRECIO_MANO_OBRA"
    assert ev["actor"] == {"id": "cajero-1", "nombre": "Cajera Ana"}
    assert ev["venta_folio"] == venta["folio"]
    assert ev["detalle"]["precio_aplicado"] == 850.0
    assert ev["detalle"]["diferencia_total"] == 850.0
    assert ev["detalle"]["origen_referencia"] == "CATALOGO"
    assert ev["revisada"] is False


def test_precio_de_catalogo_o_de_otra_lista_no_se_registra(mock_db):
    db = mock_db[f"t_{TENANT}"]
    sid = _servicio(db, precio=500.0, precio_taller=400.0)

    _vender([_linea(sid, 500.0), _linea(sid, 400.0), _linea(sid, 580.0, incluye_iva=True)])

    assert db["bitacora"].count_documents({}) == 0


def test_refaccion_no_entra_a_la_bitacora(mock_db):
    db = mock_db[f"t_{TENANT}"]
    rid = str(db["items"].insert_one({"nombre": "Filtro", "tipo": "REFACCION", "precio_venta": 100.0,
                                      "stock": 5, "maneja_inventario": True, "sucursal_id": SUCURSAL}).inserted_id)
    _vender([{"producto": {"id": rid, "nombre": "Filtro", "tipo": "REFACCION"}, "cantidad": 1, "precio_unitario": 70.0}])
    assert db["bitacora"].count_documents({}) == 0


def test_linea_de_os_se_compara_contra_el_precio_de_la_os(mock_db):
    db = mock_db[f"t_{TENANT}"]
    sid = _servicio(db, precio=0.0)
    oid = db["ordenes_servicio"].insert_one({
        "folio": "OS-77", "sucursal_id": SUCURSAL, "estado": "TERMINADO",
        "puntosArreglar": [{"nombre": "Motor", "items": [
            {"linea_id": "L1", "item_id": sid, "nombre": "Afinación mayor", "tipo": "SERVICIO",
             "precioVenta": 900.0, "piezas": 1},
        ]}],
    }).inserted_id

    # Mismo precio que la OS: no es captura manual aunque el catálogo diga $0.
    _vender([_linea(sid, 900.0, linea_id="L1")], orden_id=str(oid))
    assert db["bitacora"].count_documents({}) == 0


def test_linea_de_os_cobrada_mas_barata_registra_la_diferencia(mock_db):
    db = mock_db[f"t_{TENANT}"]
    sid = _servicio(db, precio=0.0)
    oid = db["ordenes_servicio"].insert_one({
        "folio": "OS-78", "sucursal_id": SUCURSAL, "estado": "TERMINADO",
        "puntosArreglar": [{"nombre": "Motor", "items": [
            {"linea_id": "L1", "item_id": sid, "nombre": "Afinación mayor", "tipo": "SERVICIO",
             "precioVenta": 900.0, "piezas": 1},
        ]}],
    }).inserted_id

    venta = _vender([_linea(sid, 600.0, linea_id="L1")], orden_id=str(oid))
    ev = db["bitacora"].find_one({"venta_id": venta["id"]})
    assert ev["orden_folio"] == "OS-78"
    assert ev["detalle"]["origen_referencia"] == "OS"
    assert ev["detalle"]["diferencia_total"] == -300.0


def test_solo_admin_lista_y_revisa_una_sola_vez(mock_db):
    db = mock_db[f"t_{TENANT}"]
    sid = _servicio(db, precio=0.0)
    _vender([_linea(sid, 300.0)])

    def ev(grupo, path=None, qs=None):
        e = {"requestContext": {"authorizer": {"claims": _claims(grupo)}}}
        if path:
            e["pathParameters"] = path
        if qs:
            e["queryStringParameters"] = qs
        return e

    assert list_bitacora_handler(ev("CAJERO"), None)["statusCode"] == 403

    data = json.loads(list_bitacora_handler(ev("ADMIN", qs={"revisada": "false"}), None)["body"])["data"]
    assert len(data["items"]) == 1
    evento_id = data["items"][0]["id"]

    assert revisar_bitacora_handler(ev("CAJERO", path={"id": evento_id}), None)["statusCode"] == 403
    resp = revisar_bitacora_handler(ev("ADMIN", path={"id": evento_id}), None)
    assert resp["statusCode"] == 200
    assert db["bitacora"].find_one({"_id": ObjectId(evento_id)})["revisada"] is True
    assert revisar_bitacora_handler(ev("ADMIN", path={"id": evento_id}), None)["statusCode"] == 409

    data = json.loads(list_bitacora_handler(ev("ADMIN"), None)["body"])["data"]
    assert data["pendientes"] == 0
