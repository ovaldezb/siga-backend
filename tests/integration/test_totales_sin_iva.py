"""La operación va SIN IVA: el precio capturado ES el monto a cobrar.

Estos tests fijan la convención en el cálculo de totales de OS (que cotizaciones
reusa vía _calcular_totales_orden). Antes se extraía la base dividiendo entre 1.16
cuando precio_incluye_iva era True, y eso descuadraba la OS contra el dashboard, la
caja y el P&L contable. El tratamiento fiscal del catálogo sobrevive como metadato
para el futuro módulo de facturación, pero no debe alterar ningún total.
"""
from src.handlers.ordenes.ordenes_manager import _calcular_totales_orden


def _punto(items):
    return [{"nombre": "Frenos", "items": items}]


def test_total_es_la_suma_de_los_precios_capturados():
    totales = _calcular_totales_orden(_punto([
        {"nombre": "Balatas", "piezas": 2, "precioVenta": 580.0},
        {"nombre": "Rectificado", "piezas": 1, "precioVenta": 400.0},
    ]))
    assert totales["total"] == 1560.0
    assert totales["subtotal"] == 1560.0
    assert totales["iva"] == 0.0


def test_los_flags_fiscales_no_alteran_el_total():
    """Mismo precio con cualquier combinación de flags ⇒ mismo total."""
    base = {"nombre": "Filtro", "piezas": 1, "precioVenta": 232.0}
    esperado = 232.0

    for flags in (
        {},
        {"precio_incluye_iva": True},
        {"precio_incluye_iva": False},
        {"iva_exento": True},
        {"precio_incluye_iva": True, "iva_exento": True},
    ):
        totales = _calcular_totales_orden(_punto([{**base, **flags}]))
        assert totales["total"] == esperado, flags
        assert totales["subtotal"] == esperado, flags
        assert totales["iva"] == 0.0, flags


def test_cortesias_y_rechazados_no_suman():
    totales = _calcular_totales_orden(_punto([
        {"nombre": "Balatas", "piezas": 1, "precioVenta": 800.0},
        {"nombre": "Líquido de regalo", "piezas": 1, "precioVenta": 200.0, "no_cobrar": True},
        {"nombre": "Amortiguador no autorizado", "piezas": 2, "precioVenta": 1500.0, "rechazado": True},
        {"nombre": "Sugerencia rechazada", "piezas": 1, "precioVenta": 300.0, "decision": "rechazado"},
    ]))
    assert totales["total"] == 800.0
    assert totales["iva"] == 0.0


def test_orden_vacia_da_ceros():
    for entrada in (None, [], _punto([])):
        totales = _calcular_totales_orden(entrada)
        assert totales == {"subtotal": 0.0, "iva": 0.0, "total": 0.0}


def test_precios_invalidos_se_ignoran_sin_romper():
    """Un renglón a medio capturar no debe tirar el cálculo de la OS."""
    totales = _calcular_totales_orden(_punto([
        {"nombre": "Bueno", "piezas": 1, "precioVenta": 500.0},
        {"nombre": "Sin precio", "piezas": 1},
        {"nombre": "Basura", "piezas": "dos", "precioVenta": "cien"},
    ]))
    assert totales["total"] == 500.0
