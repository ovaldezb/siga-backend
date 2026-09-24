"""Reporte del taller por periodo: GET /reportes/kpis?desde=&hasta=.

Lo que se fija aquí:
- Mismos criterios que Contabilidad: fuera ventas canceladas y las de OS canceladas.
- El periodo anterior (mismo largo) sale aparte para calcular variaciones reales.
- Servicios contra refacciones cuadra con el ingreso aunque haya descuento.
- Mecánicos, clientes, métodos de pago y cotizaciones salen del periodo.
"""
import json
from datetime import datetime

from src.handlers.admin.reportes_manager import get_kpis_handler

TENANT = "tenant-reportes"


def _db(mock_db):
    return mock_db[f"t_{TENANT.replace('-', '')}"]


def _kpis(params=None, grupos=None):
    claims = {"custom:tenant_id": TENANT}
    if grupos:
        claims["cognito:groups"] = grupos
    return get_kpis_handler({
        "queryStringParameters": params or {},
        "requestContext": {"authorizer": {"claims": claims}},
    }, None)


def _reporte(desde="2026-09-01", hasta="2026-09-30", **extra):
    resp = _kpis({"desde": desde, "hasta": hasta, **extra})
    assert resp["statusCode"] == 200, resp["body"]
    return json.loads(resp["body"])["data"]


def _linea(nombre, cantidad, precio, costo, tipo="PRODUCTO", pid=None):
    return {
        "producto": {"id": pid or nombre, "nombre": nombre, "tipo": tipo},
        "cantidad": cantidad, "precio_unitario": precio, "costo_unitario_snapshot": costo,
    }


def _venta(fecha, items, total=None, **extra):
    if total is None:
        total = sum(i["cantidad"] * i["precio_unitario"] for i in items)
    return {"createdAt": fecha, "items": items, "total": total, "tenant_id": TENANT, **extra}


def test_resumen_del_periodo_excluye_canceladas(mock_db):
    db = _db(mock_db)
    os_cancelada = db.ordenes_servicio.insert_one({"estado": "CANCELADO"}).inserted_id
    db.ventas.insert_many([
        _venta(datetime(2026, 9, 5), [_linea("Balatas", 2, 500, 300)]),               # 1000 / costo 600
        _venta(datetime(2026, 9, 6), [_linea("Afinación", 1, 1500, 0, "SERVICIO")]),  # 1500 / costo 0
        _venta(datetime(2026, 9, 7), [_linea("Filtro", 1, 999, 1)], estado="CANCELADA"),
        _venta(datetime(2026, 9, 8), [_linea("Aceite", 1, 800, 1)], orden_id=str(os_cancelada)),
        _venta(datetime(2026, 10, 1), [_linea("Fuera", 1, 700, 1)]),                  # otro mes
    ])
    r = _reporte()["resumen"]
    assert r["ingresos"] == 2500.0
    assert r["costo_venta"] == 600.0
    assert r["utilidad_bruta"] == 1900.0
    assert r["margen_pct"] == 76.0
    assert r["ventas_count"] == 2
    assert r["ticket_promedio"] == 1250.0


def test_periodo_anterior_es_del_mismo_largo(mock_db):
    db = _db(mock_db)
    db.ventas.insert_many([
        _venta(datetime(2026, 9, 10), [_linea("A", 1, 1000, 0)]),
        _venta(datetime(2026, 8, 20), [_linea("B", 1, 400, 0)]),   # dentro del anterior (10 días antes)
        _venta(datetime(2026, 8, 1), [_linea("C", 1, 9999, 0)]),   # fuera de ambos
    ])
    data = _reporte("2026-09-01", "2026-09-10")
    assert data["anterior"]["desde"].startswith("2026-08-22")
    assert data["anterior"]["hasta"].startswith("2026-08-31")
    assert data["resumen_anterior"]["ingresos"] == 0.0

    data = _reporte("2026-09-01", "2026-09-30")
    assert data["resumen_anterior"]["ingresos"] == 400.0


def test_mezcla_servicios_refacciones_cuadra_con_descuento(mock_db):
    # Líneas por 2000; la venta se cobró en 1800 por un descuento general.
    _db(mock_db).ventas.insert_one(_venta(
        datetime(2026, 9, 3),
        [_linea("Mano de obra", 1, 1000, 0, "SERVICIO"), _linea("Amortiguador", 1, 1000, 600)],
        total=1800, descuento=200,
    ))
    data = _reporte()
    mezcla = data["mezcla"]
    assert mezcla["SERVICIO"]["ingresos"] == 900.0
    assert mezcla["REFACCION"]["ingresos"] == 900.0
    assert mezcla["REFACCION"]["utilidad"] == 300.0
    assert data["resumen"]["descuentos"] == 200.0
    assert data["top_servicios"][0]["nombre"] == "Mano de obra"
    assert data["top_refacciones"][0]["nombre"] == "Amortiguador"


def test_top_clientes_y_publico_general(mock_db):
    _db(mock_db).ventas.insert_many([
        _venta(datetime(2026, 9, 2), [_linea("A", 1, 3000, 1000)], cliente_id="c1", cliente_nombre="Juan Pérez"),
        _venta(datetime(2026, 9, 9), [_linea("A", 1, 1000, 500)], cliente_id="c1", cliente_nombre="Juan Pérez"),
        _venta(datetime(2026, 9, 4), [_linea("B", 1, 500, 100)], cliente_id="PUBLICO_GENERAL"),
    ])
    top = _reporte()["top_clientes"]
    assert top[0]["nombre"] == "Juan Pérez"
    assert top[0]["visitas"] == 2
    assert top[0]["ingresos"] == 4000.0
    assert top[0]["utilidad"] == 2500.0
    assert top[0]["ticket_promedio"] == 2000.0
    assert top[0]["ultima_visita"].startswith("2026-09-09")
    assert top[1]["es_publico_general"] is True


def test_metodos_de_pago_con_credito_y_cambio(mock_db):
    _db(mock_db).ventas.insert_many([
        # Pagó 1000 en efectivo por una venta de 800: el cambio no es ingreso.
        _venta(datetime(2026, 9, 2), [_linea("A", 1, 800, 0)], pagos=[{"metodo": "efectivo", "monto": 1000}]),
        _venta(datetime(2026, 9, 3), [_linea("B", 1, 1200, 0)],
               pagos=[{"metodo": "TARJETA", "monto": 200}, {"metodo": "CREDITO", "monto": 1000}]),
        _venta(datetime(2026, 9, 4), [_linea("C", 1, 500, 0)], metodo_pago="TRANSFERENCIA"),
    ])
    metodos = {m["metodo"]: m["monto"] for m in _reporte()["metodos_pago"]}
    assert metodos == {"Efectivo": 800.0, "Tarjeta de crédito": 200.0,
                       "Crédito (por cobrar)": 1000.0, "Transferencia": 500.0}


def test_metodos_de_pago_se_acumulan_por_forma_sat(mock_db):
    """El mismo concepto con ids distintos va a un solo renglón, y dos conceptos con
    el mismo id (tarjeta crédito/débito) se separan por su código SAT."""
    db = _db(mock_db)
    db.configuracion.insert_one({"tenant_id": "x", "metodos_pago": [
        {"id": "1787351165836", "nombre": "Transferencia SPEI", "codigo_sat": "03"},
    ]})
    db.ventas.insert_many([
        _venta(datetime(2026, 9, 2), [_linea("A", 1, 300, 0)],
               pagos=[{"metodo": "1787351165836", "monto": 300}]),
        _venta(datetime(2026, 9, 3), [_linea("B", 1, 700, 0)],
               pagos=[{"metodo": "TRANSFERENCIA", "monto": 700, "forma_pago_sat": "03"}]),
        _venta(datetime(2026, 9, 4), [_linea("C", 1, 400, 0)],
               pagos=[{"metodo": "tarjeta", "monto": 400, "forma_pago_sat": "28"}]),
        _venta(datetime(2026, 9, 5), [_linea("D", 1, 600, 0)],
               pagos=[{"metodo": "tarjeta", "monto": 600, "forma_pago_sat": "04"}]),
    ])
    filas = {m["metodo"]: (m["forma_pago_sat"], m["monto"]) for m in _reporte()["metodos_pago"]}
    assert filas == {
        "Transferencia": ("03", 1000.0),
        "Tarjeta de débito": ("28", 400.0),
        "Tarjeta de crédito": ("04", 600.0),
    }


def test_mecanicos_del_periodo_y_carga_actual(mock_db):
    db = _db(mock_db)
    os1 = db.ordenes_servicio.insert_one({
        "estado": "ENTREGADO", "mecanico_id": "m1", "mecanico_nombre": "Luis",
        "createdAt": datetime(2026, 9, 1),
    }).inserted_id
    os2 = db.ordenes_servicio.insert_one({
        "estado": "ENTREGADO", "createdAt": datetime(2026, 9, 1),
    }).inserted_id
    db.ordenes_servicio.insert_one({"estado": "EN_PROCESO", "mecanico_id": "m1", "mecanico_nombre": "Luis"})
    db.ventas.insert_many([
        _venta(datetime(2026, 9, 4), [_linea("A", 1, 2000, 500)], orden_id=str(os1)),
        _venta(datetime(2026, 9, 2), [_linea("B", 1, 1000, 0)], orden_id=str(os2)),
    ])
    data = _reporte()
    luis, sin = data["mecanicos"]
    assert luis["nombre"] == "Luis"
    assert luis["os_cobradas"] == 1
    assert luis["ingresos"] == 2000.0
    assert luis["dias_estancia"] == 3.0
    assert luis["en_taller"] == 1
    assert sin["nombre"] == "Sin asignar"
    assert data["resumen"]["dias_estancia_mediana"] == 2.0


def test_embudo_de_cotizaciones_por_importe(mock_db):
    _db(mock_db).ordenes_servicio.insert_many([
        {"createdAt": datetime(2026, 9, 3), "estado": "EN_PROCESO", "puntosArreglar": [{"items": [
            {"precioVenta": 800, "piezas": 1, "aprobado": True},
            {"precioVenta": 4000, "piezas": 2, "rechazado": True},
            {"precioVenta": 300, "piezas": 1},
            {"precioVenta": 999, "piezas": 1, "aprobado": True, "no_cobrar": True},  # cortesía
        ]}]},
        {"createdAt": datetime(2026, 9, 5), "estado": "CANCELADO",
         "puntosArreglar": [{"items": [{"precioVenta": 5000, "piezas": 1, "aprobado": True}]}]},
    ])
    c = _reporte()["cotizaciones"]
    assert c["os_recibidas"] == 2
    assert c["os_canceladas"] == 1
    assert c["aprobado"] == 800.0
    assert c["rechazado"] == 8000.0
    assert c["pendiente"] == 300.0
    assert c["tasa_aprobacion_pct"] == 9.1


def test_tendencia_doce_meses_termina_en_el_mes_del_periodo(mock_db):
    _db(mock_db).ventas.insert_many([
        _venta(datetime(2026, 9, 15), [_linea("A", 1, 1000, 400)]),
        _venta(datetime(2025, 10, 3), [_linea("B", 1, 500, 100)]),
        _venta(datetime(2025, 9, 30), [_linea("C", 1, 9999, 0)]),  # 13 meses atrás: fuera
    ])
    tendencia = _reporte()["tendencia"]
    assert len(tendencia) == 12
    assert tendencia[0] == {"mes": "2025-10", "ingresos": 500.0, "utilidad": 400.0, "ventas_count": 1}
    assert tendencia[-1] == {"mes": "2026-09", "ingresos": 1000.0, "utilidad": 600.0, "ventas_count": 1}


def test_tendencia_cruza_fin_de_anio(mock_db):
    _db(mock_db).ventas.insert_one(_venta(datetime(2026, 12, 20), [_linea("A", 1, 100, 0)]))
    tendencia = _reporte("2026-12-01", "2026-12-31")["tendencia"]
    assert tendencia[-1]["mes"] == "2026-12"
    assert tendencia[-1]["ingresos"] == 100.0


def test_piezas_sin_costear_del_periodo(mock_db):
    linea = _linea("Pieza externa", 2, 350, 0)
    linea["costo_pendiente"] = True
    _db(mock_db).ventas.insert_one(_venta(datetime(2026, 9, 3), [linea]))
    assert _reporte()["costos_pendientes"] == {"count": 1, "importe_venta": 700.0}


def test_filtra_por_sucursal(mock_db):
    _db(mock_db).ventas.insert_many([
        _venta(datetime(2026, 9, 3), [_linea("A", 1, 100, 0)], sucursal_id="norte"),
        _venta(datetime(2026, 9, 3), [_linea("B", 1, 300, 0)], sucursal_id="sur"),
    ])
    assert _reporte(sucursal_id="sur")["resumen"]["ingresos"] == 300.0


def test_periodo_invalido_es_400():
    assert _kpis({"desde": "2026-09-30", "hasta": "2026-09-01"})["statusCode"] == 400
    assert _kpis({"desde": "2026-09-01"})["statusCode"] == 400
    assert _kpis({"desde": "2020-01-01", "hasta": "2026-09-01"})["statusCode"] == 400


def test_mecanico_no_ve_reportes():
    resp = _kpis({"desde": "2026-09-01", "hasta": "2026-09-30"}, grupos=["MECANICO"])
    assert resp["statusCode"] == 403

