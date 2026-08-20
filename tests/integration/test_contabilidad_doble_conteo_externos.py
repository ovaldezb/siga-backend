"""Refacciones externas de una OS: el costo se resta UNA sola vez en el P&L.

Bug reportado (venta V-009 / OS-007, horquillas): la venta cobró $4,680 con un costo
de proveedor de $1,968 ⇒ utilidad $2,712. Al cerrar la venta, ventas_manager genera
automáticamente la compra a proveedor (CxP + IVA acreditable). El resumen mensual
sumaba esa compra otra vez como "gasto operativo" — con IVA incluido — y la utilidad
del mes se comía el costo dos veces.

Estos tests recorren el flujo real (crear venta → resumen mensual) y fijan la
convención: el costo del item externo vive en `costo_venta`; la compra auto-generada
sólo aporta CxP e IVA acreditable, nunca gasto operativo.
"""
import json
from datetime import datetime

from src.handlers.ventas.ventas_manager import create_venta_handler
from src.handlers.contabilidad.contabilidad_manager import get_resumen_mensual_handler


TENANT = "tallertest"
SUCURSAL = "suc-a"

PRECIO_VENTA = 4680.0
COSTO_PROVEEDOR = 1968.0


def _claims():
    return {"custom:tenant_id": TENANT, "sub": "user-1", "email": "test@taller.com", "name": "Tester"}


def _auth_event(extra=None):
    ev = {"requestContext": {"authorizer": {"claims": _claims()}}}
    if extra:
        ev.update(extra)
    return ev


def _seed_proveedor(db):
    return str(db["proveedores"].insert_one({
        "nombre": "Refacciones del Norte",
        "rfc": "RNO010101AAA",
        "tenant_id": TENANT,
    }).inserted_id)


def _venta_con_horquillas_externas(db, proveedor_id):
    """Replica la venta del reporte: una OS con una refacción traída de proveedor."""
    orden_id = str(db["ordenes_servicio"].insert_one({
        "folio": "OS-007",
        "tenant_id": TENANT,
        "sucursal_id": SUCURSAL,
        "estado": "APROBADO",
    }).inserted_id)

    body = {
        "sucursal_id": SUCURSAL,
        "cliente_id": "PUBLICO_GENERAL",
        "orden_id": orden_id,
        "folio_orden": "OS-007",
        "items": [{
            "producto": {"id": "manual", "nombre": "Horquillas", "no_parte": "HRQ-1"},
            "cantidad": 1,
            "precio_unitario": PRECIO_VENTA,
            "es_externo": True,
            "proveedor_id": proveedor_id,
            "costo_proveedor": COSTO_PROVEEDOR,
        }],
        "metodo_pago": "EFECTIVO",
        "pagos": [{"metodo": "EFECTIVO", "monto": PRECIO_VENTA}],
    }
    resp = create_venta_handler(_auth_event({"body": json.dumps(body)}), None)
    assert resp["statusCode"] == 201, resp["body"]
    return json.loads(resp["body"])["data"]


def _resumen_del_mes_actual():
    hoy = datetime.utcnow()
    resp = get_resumen_mensual_handler(_auth_event({
        "queryStringParameters": {"year": str(hoy.year), "month": str(hoy.month)}
    }), None)
    assert resp["statusCode"] == 200, resp["body"]
    return json.loads(resp["body"])["data"]


def test_costo_externo_no_se_cuenta_dos_veces_en_el_pl(mock_db):
    db = mock_db[f"t_{TENANT}"]
    _venta_con_horquillas_externas(db, _seed_proveedor(db))

    d = _resumen_del_mes_actual()

    assert d["ingresos"]["netos"] == PRECIO_VENTA
    assert d["costo_venta"] == COSTO_PROVEEDOR
    # El costo del proveedor ya está en costo_venta: no vuelve como gasto operativo.
    assert d["gastos_variables"] == 0.0
    assert d["utilidad_bruta"] == PRECIO_VENTA - COSTO_PROVEEDOR   # 2,712
    assert d["utilidad_neta"] == PRECIO_VENTA - COSTO_PROVEEDOR    # 2,712, no 744


def test_la_compra_autogenerada_sigue_existiendo_como_cxp_e_iva(mock_db):
    """El fix no borra la compra: sólo deja de restarla dos veces."""
    db = mock_db[f"t_{TENANT}"]
    _venta_con_horquillas_externas(db, _seed_proveedor(db))

    compra = db["compras"].find_one({"origen": "VENTA_OS"})
    assert compra is not None
    assert compra["subtotal"] == COSTO_PROVEEDOR
    assert compra["saldo_pendiente"] == round(COSTO_PROVEEDOR * 1.16, 2)
    assert compra["items"][0]["en_costo_venta"] is True
    assert compra["venta_id"]

    d = _resumen_del_mes_actual()
    # El IVA de la factura del proveedor sí es acreditable real.
    assert d["iva_acreditable_compras"] == round(COSTO_PROVEEDOR * 0.16, 2)
    # Se publica aparte para trazabilidad, sin restar de la utilidad.
    assert d["compras_costo_venta_base"] == COSTO_PROVEEDOR
    assert len(d["detalle"]["compras_costo_venta"]) == 1
    assert d["detalle"]["gastos_variables"] == []


def test_gasto_operativo_real_sigue_restando(mock_db):
    """Una compra capturada a mano (no ligada a venta) sí es gasto del mes."""
    db = mock_db[f"t_{TENANT}"]
    db["compras"].insert_one({
        "folio": "C-100",
        "proveedor_id": "prov-x",
        "proveedor_snapshot": {"nombre": "Gasolinera"},
        "sucursal_id": SUCURSAL,
        "items": [{
            "nombre": "Combustible",
            "cantidad": 1,
            "costo_unitario": 500.0,
            "subtotal_linea": 500.0,
            "iva_linea": 80.0,
            "total_linea": 580.0,
            "afecta_inventario": False,
        }],
        "subtotal": 500.0,
        "iva": 80.0,
        "total": 580.0,
        "estado": "RECIBIDA",
        "tenant_id": TENANT,
        "createdAt": datetime.utcnow(),
    })

    d = _resumen_del_mes_actual()
    assert d["gastos_variables"] == 500.0
    assert d["compras_costo_venta_base"] == 0.0
    assert d["utilidad_neta"] == -500.0


def test_compra_historica_sin_flag_origen_tambien_se_excluye(mock_db):
    """Retro-compatibilidad: las compras auto-generadas antes del fix sólo traen venta_id."""
    db = mock_db[f"t_{TENANT}"]
    db["compras"].insert_one({
        "folio": "C-090",
        "proveedor_id": "prov-y",
        "proveedor_snapshot": {"nombre": "Refaccionaria vieja"},
        "sucursal_id": SUCURSAL,
        "items": [{
            "nombre": "Horquillas",
            "cantidad": 1,
            "costo_unitario": COSTO_PROVEEDOR,
            "subtotal_linea": COSTO_PROVEEDOR,
            "iva_linea": round(COSTO_PROVEEDOR * 0.16, 2),
            "total_linea": round(COSTO_PROVEEDOR * 1.16, 2),
            "afecta_inventario": False,
            # sin `en_costo_venta` ni `origen`: doc previo al fix
        }],
        "subtotal": COSTO_PROVEEDOR,
        "iva": round(COSTO_PROVEEDOR * 0.16, 2),
        "total": round(COSTO_PROVEEDOR * 1.16, 2),
        "estado": "RECIBIDA",
        "venta_id": "0123456789abcdef01234567",
        "orden_id": "0123456789abcdef01234568",
        "tenant_id": TENANT,
        "createdAt": datetime.utcnow(),
    })

    d = _resumen_del_mes_actual()
    assert d["gastos_variables"] == 0.0
    assert d["compras_costo_venta_base"] == COSTO_PROVEEDOR
