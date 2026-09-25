"""Conciliación por forma de pago SAT: cobro → caja → corte → reportes.

El caso real (Express, prod): los métodos propios del taller se guardan con un id
numérico, así que el corte mostraba renglones como "1787351150203"; tarjeta de
crédito y de débito compartían id y se sumaban juntas; y un método "crédito" con
id propio (código 99) entraba a caja como dinero cobrado sin generar CxC.
"""
import json
from datetime import datetime

from bson import ObjectId

from src.handlers.caja.caja_manager import (
    cerrar_caja_handler, get_active_caja_handler, list_arqueos_handler,
)
from src.handlers.ordenes.ordenes_manager import update_orden_handler
from src.handlers.ventas.ventas_manager import create_venta_handler, registrar_abono_handler
from src.shared.utils.formas_pago_sat import esperado_por_concepto, grupo_arqueo, etiqueta_forma_pago

TENANT = "tallertest"
SUCURSAL = "suc-a"

METODOS = [
    {"id": "efectivo", "nombre": "Efectivo", "codigo_sat": "01", "activo": True},
    {"id": "tarjeta", "nombre": "Tarjeta de crédito", "codigo_sat": "04", "activo": True},
    {"id": "1787351150203", "nombre": "Tarjeta de débito", "codigo_sat": "28", "activo": True},
    {"id": "1787351165836", "nombre": "SPEI", "codigo_sat": "03", "activo": True},
    {"id": "1787351187727", "nombre": "Crédito empresas", "codigo_sat": "99", "activo": True},
]


def _event(body=None, qs=None, path_id=None, admin=True):
    claims = {"custom:tenant_id": TENANT, "sub": "u1", "email": "caja@taller.com", "name": "Yaz"}
    if admin:
        claims["cognito:groups"] = ["ADMIN"]
    ev = {"requestContext": {"authorizer": {"claims": claims}}}
    if body is not None:
        ev["body"] = json.dumps(body)
    if qs is not None:
        ev["queryStringParameters"] = qs
    if path_id:
        ev["pathParameters"] = {"id": path_id}
    return ev


def _db(mock_db):
    return mock_db[f"t_{TENANT}"]


def _setup(db, fondo=500.0):
    db.configuracion.insert_one({"tenant_id": TENANT, "metodos_pago": METODOS})
    sesion_id = db.caja_sesiones.insert_one({
        "sucursal_id": SUCURSAL, "estado": "ABIERTA", "monto_inicial": fondo,
        "total_ventas": 0.0, "total_entradas": 0.0, "total_salidas": 0.0,
        "movimientos": [], "tenant_id": TENANT,
    }).inserted_id
    item_id = str(db["items"].insert_one({
        "nombre": "Servicio", "no_parte": "SRV", "sucursal_id": SUCURSAL, "stock": 1000,
        "precio_compra": 0.0, "costo_promedio": 0.0, "precio_venta": 100.0,
        "maneja_inventario": True, "tipo": "REFACCION", "tenant_id": TENANT,
    }).inserted_id)
    cliente_id = str(db.clientes.insert_one({
        "nombre": "SOLDI", "limite_credito": 100000.0, "tenant_id": TENANT,
    }).inserted_id)
    return sesion_id, item_id, cliente_id


def _vender(item_id, total, pagos, cliente_id="PUBLICO_GENERAL"):
    body = {
        "sucursal_id": SUCURSAL, "cliente_id": cliente_id,
        "items": [{"producto": {"id": item_id, "nombre": "Servicio", "tipo": "REFACCION"},
                   "cantidad": 1, "precio_unitario": total}],
        "metodo_pago": pagos[0]["metodo"],
        "pagos": pagos,
    }
    resp = create_venta_handler(_event(body, admin=False), None)
    assert resp["statusCode"] == 201, resp["body"]
    return json.loads(resp["body"])["data"]


# --- helpers puros ----------------------------------------------------------

def test_grupos_y_etiquetas():
    assert grupo_arqueo("01") == "efectivo"
    assert grupo_arqueo("04") == grupo_arqueo("28") == "tarjeta"
    assert grupo_arqueo("03") == "otros"
    assert grupo_arqueo("99") is None
    assert etiqueta_forma_pago("28") == "Tarjeta de débito"
    assert etiqueta_forma_pago("") == "Sin clasificar"


def test_esperado_por_concepto_entradas_manuales_son_efectivo():
    sesion = {"monto_inicial": 100, "movimientos": [
        {"tipo": "VENTA", "monto": 50, "metodo": "EFECTIVO", "forma_pago_sat": "01"},
        {"tipo": "VENTA", "monto": 70, "metodo": "TARJETA", "forma_pago_sat": "28"},
        {"tipo": "VENTA", "monto": 30, "metodo": "1787351165836"},  # sin código: se resuelve
        {"tipo": "ENTRADA", "monto": 20, "concepto": "Cambio"},
        {"tipo": "SALIDA", "monto": 10, "concepto": "Garrafón"},
    ]}
    assert esperado_por_concepto(sesion, METODOS) == {"efectivo": 160.0, "tarjeta": 70.0, "otros": 30.0}


# --- cobro ------------------------------------------------------------------

def test_metodo_propio_con_codigo_99_genera_cxc_y_no_entra_a_caja(mock_db):
    db = _db(mock_db)
    sesion_id, item_id, cliente_id = _setup(db)

    venta = _vender(item_id, 1200.0, [{"metodo": "1787351187727", "monto": 1200.0}], cliente_id)

    assert venta["monto_credito"] == 1200.0
    assert venta["saldo_pendiente"] == 1200.0
    assert venta["pagos"][0]["forma_pago_sat"] == "99"
    sesion = db.caja_sesiones.find_one({"_id": sesion_id})
    assert sesion["total_ventas"] == 0.0
    assert sesion["movimientos"] == []


def test_metodo_propio_con_codigo_99_exige_cliente(mock_db):
    db = _db(mock_db)
    _, item_id, _ = _setup(db)
    body = {
        "sucursal_id": SUCURSAL, "cliente_id": "PUBLICO_GENERAL",
        "items": [{"producto": {"id": item_id, "nombre": "Servicio", "tipo": "REFACCION"},
                   "cantidad": 1, "precio_unitario": 100.0}],
        "metodo_pago": "1787351187727",
        "pagos": [{"metodo": "1787351187727", "monto": 100.0}],
    }
    resp = create_venta_handler(_event(body, admin=False), None)
    assert resp["statusCode"] == 400


def test_movimientos_de_caja_llevan_codigo_sat(mock_db):
    db = _db(mock_db)
    sesion_id, item_id, _ = _setup(db)
    _vender(item_id, 300.0, [{"metodo": "efectivo", "monto": 100.0},
                             {"metodo": "1787351150203", "monto": 200.0}])

    movs = db.caja_sesiones.find_one({"_id": sesion_id})["movimientos"]
    assert {(m["metodo"], m["forma_pago_sat"]) for m in movs} == {("EFECTIVO", "01"), ("1787351150203", "28")}


# --- abonos -----------------------------------------------------------------

def _venta_a_credito(db, item_id, cliente_id, total=1000.0):
    return _vender(item_id, total, [{"metodo": "credito", "monto": total}], cliente_id)


def test_abono_guarda_codigo_y_entra_a_caja_con_su_concepto(mock_db):
    db = _db(mock_db)
    sesion_id, item_id, cliente_id = _setup(db)
    venta = _venta_a_credito(db, item_id, cliente_id)

    resp = registrar_abono_handler(
        _event({"monto": 400.0, "metodo": "TARJETA DE DÉBITO"}, path_id=venta["id"]), None)
    assert resp["statusCode"] == 200, resp["body"]

    abono = db.ventas.find_one({"_id": ObjectId(venta["id"])})["abonos"][0]
    assert abono["forma_pago_sat"] == "28"
    assert abono["en_caja"] is True
    sesion = db.caja_sesiones.find_one({"_id": sesion_id})
    entrada = [m for m in sesion["movimientos"] if m["tipo"] == "ENTRADA"][0]
    assert entrada["forma_pago_sat"] == "28"
    assert esperado_por_concepto(sesion, METODOS)["tarjeta"] == 400.0


def test_abono_a_credito_se_rechaza(mock_db):
    db = _db(mock_db)
    _, item_id, cliente_id = _setup(db)
    venta = _venta_a_credito(db, item_id, cliente_id)
    resp = registrar_abono_handler(
        _event({"monto": 100.0, "metodo": "1787351187727"}, path_id=venta["id"]), None)
    assert resp["statusCode"] == 400


def test_abono_con_metodo_desconocido_se_rechaza(mock_db):
    db = _db(mock_db)
    _, item_id, cliente_id = _setup(db)
    venta = _venta_a_credito(db, item_id, cliente_id)
    resp = registrar_abono_handler(
        _event({"monto": 100.0, "metodo": "CLIP"}, path_id=venta["id"]), None)
    assert resp["statusCode"] == 400


# --- corte ------------------------------------------------------------------

def test_cierre_concilia_cada_concepto(mock_db):
    """El total cuadra, pero falta efectivo y sobra terminal: el corte lo exhibe."""
    db = _db(mock_db)
    sesion_id, item_id, _ = _setup(db, fondo=500.0)
    _vender(item_id, 1000.0, [{"metodo": "efectivo", "monto": 1000.0}])
    _vender(item_id, 600.0, [{"metodo": "tarjeta", "monto": 600.0}])
    _vender(item_id, 400.0, [{"metodo": "1787351150203", "monto": 400.0}])
    _vender(item_id, 250.0, [{"metodo": "1787351165836", "monto": 250.0}])

    activa = json.loads(get_active_caja_handler(_event(qs={"sucursal_id": SUCURSAL}), None)["body"])["data"]
    assert activa["esperado_por_concepto"] == {"efectivo": 1500.0, "tarjeta": 1000.0, "otros": 250.0}

    resp = cerrar_caja_handler(_event({
        "sesion_id": str(sesion_id),
        "efectivo_fisico": 1400.0, "tarjeta_fisico": 1100.0, "otros_fisico": 250.0,
        "motivo": "revisar",
    }), None)
    assert resp["statusCode"] == 200, resp["body"]
    arqueo = json.loads(resp["body"])["data"]["arqueo"]
    assert arqueo["diferencia"] == 0.0
    assert arqueo["diferencia_por_concepto"] == {"efectivo": -100.0, "tarjeta": 100.0, "otros": 0.0}

    items = json.loads(list_arqueos_handler(_event(qs={}), None)["body"])["data"]["items"]
    assert items[0]["esperado_por_concepto"]["tarjeta"] == 1000.0


def test_cortes_viejos_se_desglosan_al_vuelo(mock_db):
    db = _db(mock_db)
    db.configuracion.insert_one({"tenant_id": TENANT, "metodos_pago": METODOS})
    db.caja_sesiones.insert_one({
        "sucursal_id": SUCURSAL, "estado": "CERRADA", "fecha_cierre": "2026-09-01T20:00:00Z",
        "monto_inicial": 0.0, "total_ventas": 300.0, "diferencia": 0.0,
        "arqueo": {"total_fisico": 300.0},
        "movimientos": [
            {"tipo": "VENTA", "monto": 100.0, "metodo": "EFECTIVO"},
            {"tipo": "VENTA", "monto": 200.0, "metodo": "1787351150203"},
        ],
    })
    items = json.loads(list_arqueos_handler(_event(qs={}), None)["body"])["data"]["items"]
    assert items[0]["esperado_por_concepto"] == {"efectivo": 100.0, "tarjeta": 200.0, "otros": 0.0}


# --- contabilidad -------------------------------------------------------------

def test_concentrado_resume_por_forma_de_pago(mock_db):
    from src.handlers.contabilidad.contabilidad_manager import get_concentrado_ventas_handler
    db = _db(mock_db)
    _, item_id, cliente_id = _setup(db)
    _vender(item_id, 1000.0, [{"metodo": "efectivo", "monto": 1000.0}])
    _vender(item_id, 400.0, [{"metodo": "1787351150203", "monto": 400.0}])
    _vender(item_id, 600.0, [{"metodo": "tarjeta", "monto": 600.0}])
    _vender(item_id, 250.0, [{"metodo": "1787351165836", "monto": 250.0}])
    _vender(item_id, 300.0, [{"metodo": "credito", "monto": 300.0}], cliente_id)

    hoy = datetime.utcnow()
    resp = get_concentrado_ventas_handler(_event(qs={"year": str(hoy.year), "month": str(hoy.month)}), None)
    assert resp["statusCode"] == 200, resp["body"]
    data = json.loads(resp["body"])["data"]
    filas = {f["forma_pago_sat"]: (f["metodo"], f["monto"]) for f in data["por_forma_pago"]}
    assert filas == {
        "01": ("Efectivo", 1000.0),
        "04": ("Tarjeta de crédito", 600.0),
        "28": ("Tarjeta de débito", 400.0),
        "03": ("Transferencia", 250.0),
        "99": ("Crédito (por cobrar)", 300.0),
    }
    assert "Tarjeta de débito" in {m for r in data["renglones"] for m in (r.get("metodos_pago") or [])}


# --- cancelación --------------------------------------------------------------

def test_cancelar_os_devuelve_cada_concepto_a_su_renglon(mock_db):
    db = _db(mock_db)
    sesion_id, _, _ = _setup(db)
    orden_id = db.ordenes_servicio.insert_one({
        "folio": "OS-0001", "tenant_id": TENANT, "sucursal_id": SUCURSAL, "estado": "ENTREGADO",
        "pagada": True, "puntosArreglar": [], "total": 1000.0, "createdAt": datetime.utcnow(),
    }).inserted_id
    venta_id = db.ventas.insert_one({
        "folio": "V-1", "tenant_id": TENANT, "sucursal_id": SUCURSAL, "orden_id": str(orden_id),
        "items": [], "total": 1000.0, "monto_credito": 0.0, "saldo_pendiente": 0.0,
        "metodo_pago": "MIXTO",
        "pagos": [{"metodo": "EFECTIVO", "monto": 300.0, "forma_pago_sat": "01"},
                  {"metodo": "TARJETA", "monto": 700.0, "forma_pago_sat": "28"}],
        "caja_movimiento_registrado": True, "caja_sesion_id": str(sesion_id),
        "createdAt": datetime.utcnow(),
    }).inserted_id
    db.ordenes_servicio.update_one({"_id": orden_id}, {"$set": {"venta_id": str(venta_id)}})

    resp = update_orden_handler(_event({"estado": "CANCELADO", "motivo_cancelacion": "x"},
                                       path_id=str(orden_id)), None)
    assert resp["statusCode"] == 200, resp["body"]

    sesion = db.caja_sesiones.find_one({"_id": sesion_id})
    salidas = {m["forma_pago_sat"]: m["monto"] for m in sesion["movimientos"] if m["tipo"] == "SALIDA"}
    assert salidas == {"01": 300.0, "28": 700.0}
    assert sesion["total_salidas"] == 1000.0
