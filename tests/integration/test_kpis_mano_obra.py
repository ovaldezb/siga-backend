"""KPI de mano de obra por mecánico: GET /kpis/mano-obra?mes=.

Lo que se fija aquí:
- Mano de obra es la línea de tipo SERVICIO; el resto cuenta como refacción.
- El ingreso es el facturado (sin importar si se pagó) y el descuento se prorratea.
- Toda la mano de obra de una OS va a su mecánico; sin OS o sin mecánico, "Sin asignar".
- Fuera canceladas; el mes anterior sale para comparar.
- Las líneas tecleadas como "MANO DE OBRA" pero guardadas como refacción se avisan aparte.
"""
import json
from datetime import datetime

from bson import ObjectId

from src.handlers.admin.kpis_manager import get_mano_obra_handler

TENANT = "tenant-kpis-mo"


def _db(mock_db):
    return mock_db[f"t_{TENANT.replace('-', '')}"]


def _pedir(params=None, grupos=None):
    claims = {"custom:tenant_id": TENANT}
    if grupos:
        claims["cognito:groups"] = grupos
    return get_mano_obra_handler({
        "queryStringParameters": params or {},
        "requestContext": {"authorizer": {"claims": claims}},
    }, None)


def _kpi(mes="2026-09", **extra):
    resp = _pedir({"mes": mes, **extra})
    assert resp["statusCode"] == 200, resp["body"]
    return json.loads(resp["body"])["data"]


def _servicio(nombre, precio, cantidad=1):
    return {"producto": {"id": nombre, "nombre": nombre, "tipo": "SERVICIO"},
            "cantidad": cantidad, "precio_unitario": precio, "costo_unitario_snapshot": 0}


def _pieza(nombre, precio, cantidad=1):
    return {"producto": {"id": "manual", "nombre": nombre, "tipo": "PRODUCTO"},
            "cantidad": cantidad, "precio_unitario": precio, "costo_unitario_snapshot": precio / 2}


def _orden(db, mecanico_id=None, nombre=None, **extra):
    doc = {"estado": "ENTREGADO", **extra}
    if mecanico_id:
        doc.update({"mecanico_id": mecanico_id, "mecanico_nombre": nombre})
    return str(db.ordenes_servicio.insert_one(doc).inserted_id)


def _venta(db, fecha, items, orden_id=None, total=None, **extra):
    if total is None:
        total = sum(i["cantidad"] * i["precio_unitario"] for i in items)
    doc = {"createdAt": fecha, "items": items, "total": total, "tenant_id": TENANT, **extra}
    if orden_id:
        doc["orden_id"] = orden_id
    db.ventas.insert_one(doc)


def _sembrar(db):
    sep = lambda dia: datetime(2026, 9, dia)
    # Ana: dos órdenes con mano de obra (1000 + 3000) y una solo de refacciones.
    _venta(db, sep(2), [_servicio("MANO DE OBRA", 1000), _pieza("Balatas", 800)], _orden(db, "m1", "Ana"))
    _venta(db, sep(5), [_servicio("Afinación", 1500, 2)], _orden(db, "m1", "Ana"), saldo_pendiente=3000)  # a crédito
    _venta(db, sep(6), [_pieza("Filtro", 500)], _orden(db, "m1", "Ana"))
    # Luis: 2000 de mano de obra con 10% de descuento general → 1800; la pieza queda en 900.
    _venta(db, sep(9), [_servicio("MANO DE OBRA", 2000), _pieza("Bomba", 1000)], _orden(db, "m2", "Luis"), total=2700)
    # Sin mecánico en la OS, y mostrador sin OS con una "mano de obra" tecleada como refacción.
    _venta(db, sep(12), [_servicio("Diagnóstico", 400)], _orden(db))
    _venta(db, sep(13), [_pieza("Mano de obra  frenos", 600), _pieza("Aceite", 300)])
    # Fuera: cancelada, de OS cancelada y de octubre.
    _venta(db, sep(20), [_servicio("X", 9999)], _orden(db, "m1", "Ana"), estado="CANCELADA")
    _venta(db, sep(21), [_servicio("Y", 9999)], _orden(db, "m1", "Ana", estado="CANCELADO"))
    _venta(db, datetime(2026, 10, 1), [_servicio("Z", 9999)], _orden(db, "m1", "Ana"))
    # Agosto: Ana 500, Carla 700 (Carla no trabajó en septiembre).
    _venta(db, datetime(2026, 8, 10), [_servicio("MANO DE OBRA", 500)], _orden(db, "m1", "Ana"))
    _venta(db, datetime(2026, 8, 11), [_servicio("MANO DE OBRA", 700)], _orden(db, "m3", "Carla"))


def test_resumen_del_taller(mock_db):
    _sembrar(_db(mock_db))
    k = _kpi()
    assert (k["mes"], k["mes_anterior"]) == ("2026-09", "2026-08")
    assert k["resumen"] == {
        "mano_obra": 6200.0, "refacciones": 3100.0, "ingresos": 9300.0, "pct_mano_obra": 66.7,
        "ordenes": 6, "ordenes_con_mano_obra": 4, "promedio_por_orden": 1550.0, "mecanicos": 2,
    }
    assert (k["anterior"]["mano_obra"], k["anterior"]["ordenes"]) == (1200.0, 2)


def test_reparto_por_mecanico(mock_db):
    _sembrar(_db(mock_db))
    ana, luis, sin = _kpi()["mecanicos"]

    # Lo facturado cuenta aunque la venta siga a crédito.
    assert (ana["nombre"], ana["mecanico_id"], ana["mano_obra"], ana["refacciones"]) == ("Ana", "m1", 4000.0, 1300.0)
    assert (ana["ordenes"], ana["ordenes_con_mano_obra"], ana["promedio_por_orden"]) == (3, 2, 2000.0)
    assert (ana["pct_del_taller"], ana["mano_obra_anterior"]) == (64.5, 500.0)

    # El descuento general de la venta se reparte entre mano de obra y refacciones.
    assert (luis["nombre"], luis["mano_obra"], luis["refacciones"], luis["pct_del_taller"]) == ("Luis", 1800.0, 900.0, 29.0)
    assert luis["mano_obra_anterior"] is None

    # OS sin mecánico y mostrador van juntos, siempre al final.
    assert (sin["nombre"], sin["mecanico_id"], sin["mano_obra"], sin["refacciones"], sin["ordenes"]) == (
        "Sin asignar", None, 400.0, 900.0, 2)


def test_servicios_y_posible_mano_obra(mock_db):
    _sembrar(_db(mock_db))
    k = _kpi()
    assert [(s["nombre"], s["ingresos"], s["cantidad"], s["lineas"]) for s in k["servicios"]] == [
        ("Afinación", 3000.0, 2.0, 1), ("MANO DE OBRA", 2800.0, 2.0, 2), ("Diagnóstico", 400.0, 1.0, 1)]
    # Solo lo que empieza con "mano de obra" y quedó como refacción; no se adivina más.
    assert k["posible_mano_obra"] == {"count": 1, "importe": 600.0}


def test_sucursal_mes_vacio_y_validaciones(mock_db):
    db = _db(mock_db)
    _venta(db, datetime(2026, 9, 3), [_servicio("MANO DE OBRA", 100)], _orden(db, "m1", "Ana"), sucursal_id="s1")
    _venta(db, datetime(2026, 9, 4), [_servicio("MANO DE OBRA", 900)], _orden(db, "m2", "Luis"), sucursal_id="s2")
    assert [m["nombre"] for m in _kpi(sucursal_id="s1")["mecanicos"]] == ["Ana"]

    vacio = _kpi(mes="2026-01")
    assert vacio["resumen"]["mano_obra"] == 0.0 and vacio["mecanicos"] == [] and vacio["servicios"] == []
    assert vacio["posible_mano_obra"] == {"count": 0, "importe": 0.0}

    assert _pedir({"mes": "septiembre"})["statusCode"] == 400
    assert _pedir({"mes": "2026-09"}, grupos=["MECANICO"])["statusCode"] == 403
    assert get_mano_obra_handler({"requestContext": {"authorizer": {"claims": {}}}}, None)["statusCode"] == 403


def test_mecanico_sin_nombre_en_la_os(mock_db):
    db = _db(mock_db)
    # `mecanico_id` en la OS es el `_id` del usuario en hex. El `id` guardado (como
    # traía el ADMIN inicial) no cuenta.
    rosa = str(db.usuarios.insert_one({"id": "rosa@taller.com", "nombre": "Rosa", "apellido": "Lara"}).inserted_id)
    borrado = str(ObjectId())
    _venta(db, datetime(2026, 9, 3), [_servicio("MANO DE OBRA", 300)], _orden(db, mecanico_id=rosa, nombre=None))
    _venta(db, datetime(2026, 9, 4), [_servicio("MANO DE OBRA", 200)], _orden(db, mecanico_id=borrado, nombre=""))
    # El texto "null" guardado como id no es un mecánico.
    _venta(db, datetime(2026, 9, 5), [_servicio("MANO DE OBRA", 100)], _orden(db, mecanico_id="null", nombre=""))
    assert [(m["mecanico_id"], m["nombre"]) for m in _kpi()["mecanicos"]] == [
        (rosa, "Rosa Lara"), (borrado, "Mecánico sin nombre"), (None, "Sin asignar")]

