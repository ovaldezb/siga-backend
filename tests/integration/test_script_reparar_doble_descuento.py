"""El script de reparación toca stock de producción: su detección va probada.

Cubre que cuente sólo lo realmente duplicado (OS + venta de la misma orden), que
no invente diferencias donde hubo un solo descuento, y que no repare dos veces.
"""
import importlib.util
from pathlib import Path

from bson import ObjectId

TENANT = "tallertest"
_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "reparar_doble_descuento_inventario.py"


def _cargar_script():
    spec = importlib.util.spec_from_file_location("reparar_doble_descuento", _SCRIPT)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def _seed(db, *, piezas_os, piezas_venta, stock=8, misma_orden=True):
    """Arma una OS cobrada con sus movimientos de inventario en la bitácora."""
    item_id = str(db["items"].insert_one({
        "nombre": "Balata delantera", "stock": stock, "sucursal_id": "suc-a",
    }).inserted_id)
    orden_id = str(db["ordenes_servicio"].insert_one({"folio": "OS-1"}).inserted_id)
    venta_id = str(db["ventas"].insert_one({
        "folio": "V-1", "orden_id": orden_id if misma_orden else str(ObjectId()),
    }).inserted_id)

    if piezas_os:
        db["inventario_movimientos"].insert_one({
            "item_id": item_id, "item_nombre": "Balata delantera", "cantidad": -piezas_os,
            "concepto": "CONSUMO", "referencia_id": orden_id,
        })
    if piezas_venta:
        db["inventario_movimientos"].insert_one({
            "item_id": item_id, "item_nombre": "Balata delantera", "cantidad": -piezas_venta,
            "concepto": "VENTA", "referencia_id": venta_id,
        })
    return item_id, orden_id


def test_detecta_las_piezas_descontadas_dos_veces(mock_db):
    db = mock_db[f"t_{TENANT}"]
    script = _cargar_script()
    item_id, _ = _seed(db, piezas_os=2, piezas_venta=2)

    duplicados, detalle = script.detectar(db)

    assert duplicados == {item_id: 2}
    assert detalle[0]["folio"] == "OS-1"
    assert detalle[0]["duplicadas"] == 2


def test_venta_sin_consumo_en_os_no_es_duplicado(mock_db):
    """Pieza cobrada sin marcarse entregada: un solo descuento, nada que reponer."""
    db = mock_db[f"t_{TENANT}"]
    script = _cargar_script()
    _seed(db, piezas_os=0, piezas_venta=2)

    duplicados, detalle = script.detectar(db)

    assert duplicados == {}
    assert detalle == []


def test_consumo_de_una_os_no_cobrada_no_es_duplicado(mock_db):
    """El movimiento de venta pertenece a otra orden: no se cruzan."""
    db = mock_db[f"t_{TENANT}"]
    script = _cargar_script()
    _seed(db, piezas_os=2, piezas_venta=2, misma_orden=False)

    duplicados, _ = script.detectar(db)

    assert duplicados == {}


def test_cantidades_distintas_reponen_solo_el_traslape(mock_db):
    """Se surtieron 3 y se cobraron 2: sólo 2 salieron dos veces."""
    db = mock_db[f"t_{TENANT}"]
    script = _cargar_script()
    item_id, _ = _seed(db, piezas_os=3, piezas_venta=2)

    duplicados, _ = script.detectar(db)

    assert duplicados == {item_id: 2}


def test_reparar_repone_stock_y_es_idempotente(mock_db):
    db = mock_db[f"t_{TENANT}"]
    script = _cargar_script()
    item_id, _ = _seed(db, piezas_os=2, piezas_venta=2, stock=8)

    duplicados, _ = script.detectar(db)
    script.reparar(db, duplicados, "test")

    assert db["items"].find_one({"_id": ObjectId(item_id)})["stock"] == 10
    ajuste = db["inventario_movimientos"].find_one({"concepto": script.CONCEPTO_AJUSTE})
    assert ajuste["cantidad"] == 2
    assert ajuste["stock_resultante"] == 10

    # Segunda corrida: la diferencia ya está repuesta, no vuelve a sumar.
    duplicados_2, _ = script.detectar(db)
    assert duplicados_2 == {}
    assert db["items"].find_one({"_id": ObjectId(item_id)})["stock"] == 10
