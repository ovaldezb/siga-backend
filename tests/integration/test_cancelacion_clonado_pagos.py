"""Cancelación de OS cobradas, clonado de OS canceladas y corrección del método de pago.

Cubre los tres candados nuevos:
  · Cancelar una OS ya cobrada anula su venta (deja de ser ingreso) y devuelve el
    dinero a la caja abierta.
  · Contabilidad ignora las ventas cuya OS está cancelada, incluso las que quedaron
    vivas antes de que existiera la anulación automática.
  · Clonar una orden cancelada sólo se puede una vez y deja rastro de quién lo hizo.
  · El método de pago se puede corregir; los importes NO.
"""
import json
from datetime import datetime

from bson import ObjectId

from src.handlers.ordenes.ordenes_manager import update_orden_handler, clonar_orden_handler
from src.handlers.ventas.ventas_manager import update_metodo_pago_handler
from src.handlers.contabilidad.contabilidad_manager import get_resumen_mensual_handler

TENANT = "tallertest"
SUCURSAL = "suc-a"


def _claims(admin=True):
    claims = {
        "custom:tenant_id": TENANT,
        "sub": "user-1",
        "email": "admin@taller.com",
        "name": "Admin Tester",
    }
    if admin:
        claims["cognito:groups"] = ["ADMIN"]
    return claims


def _event(*, path_id=None, body=None, qs=None, admin=True):
    ev = {"requestContext": {"authorizer": {"claims": _claims(admin)}}}
    if path_id:
        ev["pathParameters"] = {"id": path_id}
    if body is not None:
        ev["body"] = json.dumps(body)
    if qs is not None:
        ev["queryStringParameters"] = qs
    return ev


def _seed_os_cobrada(db, *, total=1000.0, credito=0.0, con_caja=True):
    """OS ENTREGADO con su venta, tal como las deja el Punto de Venta."""
    sesion_id = None
    if con_caja:
        sesion_id = db.caja_sesiones.insert_one({
            "sucursal_id": SUCURSAL,
            "estado": "ABIERTA",
            "monto_inicial": 500.0,
            "total_ventas": total - credito,
            "total_entradas": 0.0,
            "total_salidas": 0.0,
            "movimientos": [],
            "tenant_id": TENANT,
        }).inserted_id

    orden_id = db["ordenes_servicio"].insert_one({
        "folio": "OS-0100",
        "tenant_id": TENANT,
        "sucursal_id": SUCURSAL,
        "estado": "ENTREGADO",
        "pagada": credito == 0,
        "saldo_pendiente": credito,
        "cliente_snapshot": {"id": "cli-1", "nombre": "Juan", "apellido_paterno": "Pérez"},
        "vehiculo_id": "veh-1",
        "vehiculo_snapshot": {"placas": "ABC-123", "marca": "Nissan", "modelo": "Versa"},
        "puntosArreglar": [{
            "nombre": "Servicio mayor",
            "items": [{
                "linea_id": "l1", "item_id": "manual", "nombre": "Mano de obra",
                "piezas": 1, "precioVenta": total, "precioCompra": 0.0,
                "aprobado": True, "entregado": True,
                "inventario_descontado_piezas": 0, "inventario_descontado": False,
            }],
        }],
        "total": total,
        "anticipo": 0,
        "createdAt": datetime.utcnow(),
    }).inserted_id

    venta_id = db["ventas"].insert_one({
        "folio": "V-0100",
        "tenant_id": TENANT,
        "sucursal_id": SUCURSAL,
        "cliente_id": "cli-1",
        "cliente_nombre": "Juan Pérez",
        "orden_id": str(orden_id),
        "items": [{"producto": {"id": "manual", "nombre": "Mano de obra"},
                   "cantidad": 1, "precio_unitario": total,
                   "costo_unitario_snapshot": 0.0}],
        "subtotal": total, "iva": 0.0, "total": total, "descuento": 0.0,
        "metodo_pago": "EFECTIVO",
        "pagos": [{"metodo": "EFECTIVO", "monto": total - credito, "referencia": ""}],
        "monto_credito": credito,
        "saldo_pendiente": credito,
        "caja_movimiento_registrado": con_caja,
        "caja_sesion_id": str(sesion_id) if sesion_id else None,
        "createdAt": datetime.utcnow(),
    }).inserted_id

    db["ordenes_servicio"].update_one(
        {"_id": orden_id},
        {"$set": {"venta_id": str(venta_id), "venta_folio": "V-0100"}},
    )
    return str(orden_id), str(venta_id), sesion_id


# ---------------------------------------------------------------------------
# 1. Cancelar una OS cobrada
# ---------------------------------------------------------------------------

def test_cancelar_os_cobrada_anula_la_venta(mock_db):
    db = mock_db[f"t_{TENANT}"]
    orden_id, venta_id, sesion_id = _seed_os_cobrada(db, total=1000.0)

    resp = update_orden_handler(
        _event(path_id=orden_id,
               body={"estado": "CANCELADO", "motivo_cancelacion": "Error de captura"}),
        None)
    assert resp["statusCode"] == 200, resp["body"]

    venta = db["ventas"].find_one({"_id": ObjectId(venta_id)})
    assert venta["estado"] == "CANCELADA"
    assert venta["saldo_pendiente"] == 0
    assert venta["cancelada_por"] == "Admin Tester"

    orden = db["ordenes_servicio"].find_one({"_id": ObjectId(orden_id)})
    assert orden["estado"] == "CANCELADO"
    assert orden["pagada"] is False
    assert orden["venta_anulada"] is True


def test_cancelar_os_cobrada_devuelve_el_dinero_a_la_caja(mock_db):
    db = mock_db[f"t_{TENANT}"]
    orden_id, _, sesion_id = _seed_os_cobrada(db, total=1000.0)

    update_orden_handler(
        _event(path_id=orden_id, body={"estado": "CANCELADO", "motivo_cancelacion": "x"}), None)

    sesion = db.caja_sesiones.find_one({"_id": sesion_id})
    assert sesion["total_salidas"] == 1000.0
    salidas = [m for m in sesion["movimientos"] if m["tipo"] == "SALIDA"]
    assert len(salidas) == 1
    assert salidas[0]["monto"] == 1000.0
    assert "V-0100" in salidas[0]["concepto"]


def test_cancelar_os_a_credito_libera_el_saldo_sin_tocar_caja(mock_db):
    """Lo que nunca entró al cajón no puede salir: sólo se libera la cuenta por cobrar."""
    db = mock_db[f"t_{TENANT}"]
    orden_id, venta_id, sesion_id = _seed_os_cobrada(db, total=800.0, credito=800.0)
    db["ventas"].update_one({"_id": ObjectId(venta_id)},
                            {"$set": {"caja_movimiento_registrado": False}})

    update_orden_handler(
        _event(path_id=orden_id, body={"estado": "CANCELADO", "motivo_cancelacion": "x"}), None)

    venta = db["ventas"].find_one({"_id": ObjectId(venta_id)})
    assert venta["estado"] == "CANCELADA"
    assert venta["saldo_pendiente"] == 0
    assert venta["saldo_pendiente_anulado"] == 800.0
    assert db.caja_sesiones.find_one({"_id": sesion_id})["total_salidas"] == 0.0


def test_cancelar_os_no_cobrada_no_falla(mock_db):
    """La mayoría de las cancelaciones son de OS sin venta: el flujo sigue igual."""
    db = mock_db[f"t_{TENANT}"]
    orden_id = str(db["ordenes_servicio"].insert_one({
        "folio": "OS-0200", "tenant_id": TENANT, "sucursal_id": SUCURSAL,
        "estado": "COTIZADO", "puntosArreglar": [], "total": 0,
        "cliente_snapshot": {}, "createdAt": datetime.utcnow(),
    }).inserted_id)

    resp = update_orden_handler(
        _event(path_id=orden_id, body={"estado": "CANCELADO", "motivo_cancelacion": "no vino"}),
        None)
    assert resp["statusCode"] == 200
    assert db["ordenes_servicio"].find_one({"_id": ObjectId(orden_id)})["estado"] == "CANCELADO"


# ---------------------------------------------------------------------------
# 2. Contabilidad: una OS cancelada no es ganancia
# ---------------------------------------------------------------------------

def test_resumen_mensual_ignora_ventas_de_os_cancelada(mock_db):
    """Cubre las cancelaciones históricas: la OS quedó CANCELADO y la venta viva."""
    db = mock_db[f"t_{TENANT}"]
    orden_id, venta_id, _ = _seed_os_cobrada(db, total=1000.0, con_caja=False)
    # Estado que dejaba el bug: OS cancelada a mano, venta intacta.
    db["ordenes_servicio"].update_one({"_id": ObjectId(orden_id)},
                                      {"$set": {"estado": "CANCELADO"}})

    hoy = datetime.utcnow()
    resp = get_resumen_mensual_handler(
        _event(qs={"year": str(hoy.year), "month": str(hoy.month)}), None)
    assert resp["statusCode"] == 200, resp["body"]

    data = json.loads(resp["body"])["data"]
    assert data["ingresos"]["netos"] == 0.0
    assert data["ingresos"]["ventas_count"] == 0


def test_resumen_mensual_sigue_contando_las_ventas_vigentes(mock_db):
    db = mock_db[f"t_{TENANT}"]
    _seed_os_cobrada(db, total=1500.0, con_caja=False)

    hoy = datetime.utcnow()
    resp = get_resumen_mensual_handler(
        _event(qs={"year": str(hoy.year), "month": str(hoy.month)}), None)
    data = json.loads(resp["body"])["data"]
    assert data["ingresos"]["netos"] == 1500.0


# ---------------------------------------------------------------------------
# 3. Clonado de OS canceladas
# ---------------------------------------------------------------------------

def _seed_os_cancelada(db, folio="OS-0300"):
    return str(db["ordenes_servicio"].insert_one({
        "folio": folio, "tenant_id": TENANT, "sucursal_id": SUCURSAL,
        "estado": "CANCELADO", "motivo_cancelacion": "Se capturó el vehículo equivocado",
        "cliente_snapshot": {"id": "cli-1", "nombre": "Juan", "apellido_paterno": "Pérez"},
        "vehiculo_id": "veh-1",
        "vehiculo_snapshot": {"placas": "ABC-123", "marca": "Nissan"},
        "puntosArreglar": [{
            "nombre": "Frenos",
            "items": [{
                "linea_id": "vieja-1", "item_id": "manual", "nombre": "Balatas",
                "piezas": 2, "precioVenta": 500.0, "precioCompra": 200.0,
                "aprobado": True, "entregado": True,
                "inventario_descontado": True, "inventario_descontado_piezas": 2,
                "inventario_descontado_por": "OS",
            }],
        }],
        "total": 1000.0, "anticipo": 300.0, "kilometraje": 90000,
        "falla_reportada": "Rechinan los frenos",
        "createdAt": datetime.utcnow(),
    }).inserted_id)


def test_clonar_os_cancelada_genera_borrador_con_trazabilidad(mock_db):
    db = mock_db[f"t_{TENANT}"]
    orden_id = _seed_os_cancelada(db)

    resp = clonar_orden_handler(_event(path_id=orden_id, body={}), None)
    assert resp["statusCode"] == 201, resp["body"]

    nueva = json.loads(resp["body"])["data"]
    assert nueva["estado"] == "RECEPCION"
    assert nueva["folio"] != "OS-0300"
    assert nueva["clonada_de_folio"] == "OS-0300"
    assert nueva["clonada_de_orden_id"] == orden_id
    assert nueva["clonada_por"] == "Admin Tester"
    assert nueva["clonada_de_motivo_cancelacion"] == "Se capturó el vehículo equivocado"
    # No hereda dinero ni cobro
    assert nueva["anticipo"] == 0
    assert nueva["pagada"] is False
    assert nueva.get("cita_id") is None
    assert nueva["total"] == 1000.0  # los conceptos sí se conservan

    # Las líneas nacen sin historial de almacén ni de entrega
    linea = nueva["puntosArreglar"][0]["items"][0]
    assert linea["entregado"] is False
    assert linea["inventario_descontado"] is False
    assert linea["inventario_descontado_piezas"] == 0
    assert linea["linea_id"] != "vieja-1"

    original = db["ordenes_servicio"].find_one({"_id": ObjectId(orden_id)})
    assert original["clonada_en_folio"] == nueva["folio"]


def test_clonar_os_cancelada_solo_una_vez(mock_db):
    db = mock_db[f"t_{TENANT}"]
    orden_id = _seed_os_cancelada(db)

    primera = clonar_orden_handler(_event(path_id=orden_id, body={}), None)
    assert primera["statusCode"] == 201

    segunda = clonar_orden_handler(_event(path_id=orden_id, body={}), None)
    assert segunda["statusCode"] == 409
    assert "ya fue clonada" in json.loads(segunda["body"])["message"]


def test_no_se_puede_clonar_una_os_viva(mock_db):
    db = mock_db[f"t_{TENANT}"]
    orden_id = str(db["ordenes_servicio"].insert_one({
        "folio": "OS-0400", "tenant_id": TENANT, "sucursal_id": SUCURSAL,
        "estado": "EN_PROCESO", "puntosArreglar": [], "total": 0,
        "cliente_snapshot": {}, "createdAt": datetime.utcnow(),
    }).inserted_id)

    resp = clonar_orden_handler(_event(path_id=orden_id, body={}), None)
    assert resp["statusCode"] == 409


def test_clonado_deja_evento_de_auditoria(mock_db):
    db = mock_db[f"t_{TENANT}"]
    orden_id = _seed_os_cancelada(db)
    clonar_orden_handler(_event(path_id=orden_id, body={}), None)

    eventos = list(db.os_events.find({"tipo": "os.cloned"}))
    assert len(eventos) == 2  # uno en la copia, otro en la orden original
    assert all(e["actor"] == "admin@taller.com" for e in eventos)


# ---------------------------------------------------------------------------
# 4. Corrección del método de pago
# ---------------------------------------------------------------------------

def test_corregir_metodo_pago_no_mueve_importes(mock_db):
    db = mock_db[f"t_{TENANT}"]
    _, venta_id, sesion_id = _seed_os_cobrada(db, total=1000.0)
    db.caja_sesiones.update_one({"_id": sesion_id}, {"$push": {"movimientos": {
        "id": "m1", "tipo": "VENTA", "monto": 1000.0, "metodo": "EFECTIVO",
        "concepto": "Venta V-0100 (EFECTIVO)", "venta_id": venta_id, "venta_folio": "V-0100",
    }}})

    # El body intenta colar un monto distinto: debe ignorarse.
    resp = update_metodo_pago_handler(_event(path_id=venta_id, body={
        "pagos": [{"metodo": "tarjeta", "referencia": "4321", "monto": 99999}],
        "motivo": "El cliente pagó con terminal",
    }), None)
    assert resp["statusCode"] == 200, resp["body"]

    venta = db["ventas"].find_one({"_id": ObjectId(venta_id)})
    assert venta["metodo_pago"] == "TARJETA"
    assert venta["pagos"][0]["metodo"] == "TARJETA"
    assert venta["pagos"][0]["referencia"] == "4321"
    assert venta["pagos"][0]["monto"] == 1000.0   # el importe NO cambió
    assert venta["total"] == 1000.0
    assert venta["historial_metodo_pago"][0]["antes"]["metodo_pago"] == "EFECTIVO"

    # La caja abierta se re-etiqueta sin alterar el importe del movimiento.
    sesion = db.caja_sesiones.find_one({"_id": sesion_id})
    mov = next(m for m in sesion["movimientos"] if m.get("id") == "m1")
    assert mov["metodo"] == "TARJETA"
    assert mov["monto"] == 1000.0
    assert sesion["total_ventas"] == 1000.0


def test_corregir_metodo_pago_exige_administrador(mock_db):
    db = mock_db[f"t_{TENANT}"]
    _, venta_id, _ = _seed_os_cobrada(db)

    resp = update_metodo_pago_handler(
        _event(path_id=venta_id, body={"pagos": [{"metodo": "TARJETA"}]}, admin=False), None)
    assert resp["statusCode"] == 403


def test_corregir_metodo_pago_bloquea_conversion_a_credito(mock_db):
    db = mock_db[f"t_{TENANT}"]
    _, venta_id, _ = _seed_os_cobrada(db)

    resp = update_metodo_pago_handler(
        _event(path_id=venta_id, body={"pagos": [{"metodo": "CREDITO"}]}), None)
    assert resp["statusCode"] == 409


def test_corregir_metodo_pago_exige_el_mismo_numero_de_lineas(mock_db):
    db = mock_db[f"t_{TENANT}"]
    _, venta_id, _ = _seed_os_cobrada(db)

    resp = update_metodo_pago_handler(_event(path_id=venta_id, body={"pagos": [
        {"metodo": "EFECTIVO"}, {"metodo": "TARJETA"},
    ]}), None)
    assert resp["statusCode"] == 400


def test_no_se_corrige_el_pago_de_una_venta_cancelada(mock_db):
    db = mock_db[f"t_{TENANT}"]
    _, venta_id, _ = _seed_os_cobrada(db)
    db["ventas"].update_one({"_id": ObjectId(venta_id)}, {"$set": {"estado": "CANCELADA"}})

    resp = update_metodo_pago_handler(
        _event(path_id=venta_id, body={"pagos": [{"metodo": "TARJETA"}]}), None)
    assert resp["statusCode"] == 409
