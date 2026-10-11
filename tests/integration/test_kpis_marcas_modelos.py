"""KPI de mezcla de marcas y modelos: GET /kpis/marcas-modelos?mes=.

Lo que se fija aquí:
- La utilidad sale con los criterios de Reportes: fuera canceladas, ingreso sin IVA.
- El vehículo es el de la OS de la venta; el mostrador se reporta aparte.
- Marcas ordenadas por utilidad, no por cantidad, con su participación en ambas.
- Modelos y refacciones de cada marca, y la comparación con el mes anterior.
"""
import json
from datetime import datetime

from src.handlers.admin.kpis_manager import get_marcas_modelos_handler

TENANT = "tenant-kpis"


def _db(mock_db):
    return mock_db[f"t_{TENANT.replace('-', '')}"]


def _pedir(params=None, grupos=None):
    claims = {"custom:tenant_id": TENANT}
    if grupos:
        claims["cognito:groups"] = grupos
    return get_marcas_modelos_handler({
        "queryStringParameters": params or {},
        "requestContext": {"authorizer": {"claims": claims}},
    }, None)


def _kpi(mes="2026-09", **extra):
    resp = _pedir({"mes": mes, **extra})
    assert resp["statusCode"] == 200, resp["body"]
    return json.loads(resp["body"])["data"]


def _linea(nombre, cantidad, precio, costo, tipo="PRODUCTO"):
    return {
        "producto": {"id": nombre, "nombre": nombre, "tipo": tipo},
        "cantidad": cantidad, "precio_unitario": precio, "costo_unitario_snapshot": costo,
    }


def _orden(db, marca, modelo, vehiculo_id, **extra):
    return str(db.ordenes_servicio.insert_one({
        "estado": "ENTREGADO", "vehiculo_id": vehiculo_id,
        "vehiculo_snapshot": {"marca": marca, "modelo": modelo}, **extra,
    }).inserted_id)


def _venta(db, fecha, items, orden_id=None, **extra):
    total = sum(i["cantidad"] * i["precio_unitario"] for i in items)
    doc = {"createdAt": fecha, "items": items, "total": total, "tenant_id": TENANT, **extra}
    if orden_id:
        doc["orden_id"] = orden_id
    db.ventas.insert_one(doc)


def _sembrar(db):
    sep = lambda dia: datetime(2026, 9, dia)
    # Nissan: 3 órdenes, 2 vehículos, poca utilidad (300 + 200 + 100 = 600 sobre 3000).
    versa = _orden(db, "Nissan", "Versa", "v1")
    _venta(db, sep(2), [_linea("Balatas", 2, 500, 350)], versa)                     # 1000 / 700
    _venta(db, sep(9), [_linea("Balatas", 2, 500, 400)], _orden(db, "NISSAN", "versa", "v1"))  # 1000 / 800
    _venta(db, sep(12), [_linea("Filtro", 1, 1000, 900)], _orden(db, "nissan", "Sentra", "v2"))  # 1000 / 900
    # BMW: 1 orden, mucha utilidad (5000 - 1000 = 4000), mitad mano de obra.
    _venta(db, sep(15), [_linea("Bomba de agua", 1, 2500, 1000), _linea("Mano de obra", 1, 2500, 0, "SERVICIO")],
           _orden(db, "BMW", "X3", "v3"))
    # Sin marca en la OS, y venta de mostrador sin OS.
    _venta(db, sep(18), [_linea("Aceite", 1, 400, 300)], _orden(db, "", "", "v4"))   # 400 / 300
    _venta(db, sep(20), [_linea("Limpiaparabrisas", 2, 150, 100)])                   # 300 / 200
    # Fuera: cancelada, de OS cancelada y de octubre.
    _venta(db, sep(21), [_linea("X", 1, 9999, 1)], _orden(db, "Audi", "A4", "v5"), estado="CANCELADA")
    _venta(db, sep(22), [_linea("Y", 1, 9999, 1)], _orden(db, "Audi", "A4", "v5", estado="CANCELADO"))
    _venta(db, datetime(2026, 10, 1), [_linea("Z", 1, 9999, 1)], _orden(db, "Audi", "A4", "v5"))
    # Agosto (mes anterior): una de Nissan y una de Toyota.
    _venta(db, datetime(2026, 8, 10), [_linea("Balatas", 1, 800, 300)], _orden(db, "Nissan", "Versa", "v1"))  # util 500
    _venta(db, datetime(2026, 8, 11), [_linea("Clutch", 1, 3000, 2000)], _orden(db, "Toyota", "Hilux", "v6"))  # util 1000


def test_resumen_y_mostrador_aparte(mock_db):
    _sembrar(_db(mock_db))
    k = _kpi()
    assert (k["mes"], k["mes_anterior"]) == ("2026-09", "2026-08")
    assert k["resumen"] == {"ordenes": 5, "ingresos": 8400.0, "utilidad": 4700.0, "margen_pct": 56.0,
                            "marcas": 3, "modelos": 4}
    assert k["anterior"] == {"ordenes": 2, "ingresos": 3800.0, "utilidad": 1500.0, "margen_pct": 39.5}
    assert k["sin_vehiculo"] == {"ordenes": 1, "ingresos": 300.0, "utilidad": 100.0, "margen_pct": 33.3}


def test_marcas_por_utilidad_no_por_cantidad(mock_db):
    _sembrar(_db(mock_db))
    marcas = _kpi()["marcas"]
    # BMW va primero con una sola orden; "Sin marca" siempre al final.
    assert [m["marca"] for m in marcas] == ["BMW", "Nissan", "Sin marca"]

    bmw, nissan, sin_marca = marcas
    assert (bmw["ordenes"], bmw["utilidad"], bmw["margen_pct"], bmw["utilidad_por_orden"]) == (1, 4000.0, 80.0, 4000.0)
    assert (bmw["pct_ordenes"], bmw["pct_utilidad"]) == (20.0, 85.1)
    assert bmw["anterior"] is None

    # Tres formas de escribir la marca son una sola; se muestra la más usada.
    assert (nissan["ordenes"], nissan["vehiculos"], nissan["ingresos"], nissan["utilidad"]) == (3, 2, 3000.0, 600.0)
    assert (nissan["margen_pct"], nissan["utilidad_por_orden"]) == (20.0, 200.0)
    assert (nissan["pct_ordenes"], nissan["pct_utilidad"]) == (60.0, 12.8)
    assert nissan["anterior"] == {"ordenes": 1, "utilidad": 500.0}

    assert (sin_marca["ordenes"], sin_marca["utilidad"]) == (1, 100.0)
    assert sin_marca["modelos"][0]["modelo"] == "Sin modelo"


def test_modelos_y_refacciones_de_la_marca(mock_db):
    _sembrar(_db(mock_db))
    k = _kpi()
    nissan = next(m for m in k["marcas"] if m["marca"] == "Nissan")
    assert nissan["modelos_total"] == 2
    assert [(m["modelo"], m["ordenes"], m["utilidad"]) for m in nissan["modelos"]] == [
        ("Versa", 2, 500.0), ("Sentra", 1, 100.0)]
    assert [(r["nombre"], r["cantidad"], r["ingresos"], r["utilidad"]) for r in nissan["refacciones"]] == [
        ("Balatas", 4.0, 2000.0, 500.0), ("Filtro", 1.0, 1000.0, 100.0)]

    # La mano de obra no es refacción.
    bmw = next(m for m in k["marcas"] if m["marca"] == "BMW")
    assert [r["nombre"] for r in bmw["refacciones"]] == ["Bomba de agua"]

    assert [(m["marca"], m["modelo"], m["utilidad"]) for m in k["modelos"]] == [
        ("BMW", "X3", 4000.0), ("Nissan", "Versa", 500.0), ("Nissan", "Sentra", 100.0),
        ("Sin marca", "Sin modelo", 100.0)]


def test_marca_en_perdida_no_descuadra_la_participacion(mock_db):
    db = _db(mock_db)
    _venta(db, datetime(2026, 9, 3), [_linea("A", 1, 1000, 1500)], _orden(db, "Fiat", "Uno", "f1"))   # -500
    _venta(db, datetime(2026, 9, 4), [_linea("B", 1, 1000, 0)], _orden(db, "Mazda", "3", "m1"))       # 1000
    mazda, fiat = _kpi()["marcas"]
    assert (mazda["marca"], mazda["pct_utilidad"]) == ("Mazda", 100.0)
    assert (fiat["utilidad"], fiat["pct_utilidad"], fiat["margen_pct"]) == (-500.0, 0.0, -50.0)


def test_filtro_de_sucursal_mes_vacio_y_costos_pendientes(mock_db):
    db = _db(mock_db)
    _venta(db, datetime(2026, 9, 3), [{**_linea("Pieza", 1, 700, 0), "costo_pendiente": True}],
           _orden(db, "Kia", "Rio", "k1"), sucursal_id="s1")
    _venta(db, datetime(2026, 9, 4), [_linea("B", 1, 1000, 0)], _orden(db, "Mazda", "3", "m1"), sucursal_id="s2")

    k = _kpi(sucursal_id="s1")
    assert [m["marca"] for m in k["marcas"]] == ["Kia"]
    assert k["costos_pendientes"] == {"count": 1, "importe_venta": 700.0}

    vacio = _kpi(mes="2026-01")
    assert vacio["resumen"]["ordenes"] == 0 and vacio["marcas"] == [] and vacio["modelos"] == []
    assert vacio["mes_anterior"] == "2025-12"


def test_validaciones(mock_db):
    assert _pedir({"mes": "septiembre"})["statusCode"] == 400
    assert _pedir({"mes": "2026-13"})["statusCode"] == 400
    assert _pedir({"mes": "2026-09"}, grupos=["MECANICO"])["statusCode"] == 403
    assert get_marcas_modelos_handler({"requestContext": {"authorizer": {"claims": {}}}}, None)["statusCode"] == 403
    # Sin `mes` responde el mes en curso.
    assert _pedir()["statusCode"] == 200
