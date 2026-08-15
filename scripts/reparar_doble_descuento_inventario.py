"""
reparar_doble_descuento_inventario.py — Mide y repone el inventario que se
descontó dos veces por la misma pieza.

Hasta el fix de agosto 2026, una refacción salía dos veces del almacén:
  1. al marcarla "entregado" en la OS  → movimiento CONSUMO (referencia = OS)
  2. al cobrar esa misma OS en el POS  → movimiento VENTA   (referencia = venta)

El script cruza la bitácora `inventario_movimientos`: por cada OS con venta
registrada, compara las piezas que salieron por CONSUMO contra las que salieron
por VENTA del mismo item. El mínimo de ambas es la cantidad descontada de más.

Uso:
  # Sólo medir, no toca nada (empieza SIEMPRE por aquí):
  python scripts/reparar_doble_descuento_inventario.py --tenant <TENANT_ID>
  python scripts/reparar_doble_descuento_inventario.py --todos

  # Reponer el stock duplicado y dejar rastro en el kardex:
  python scripts/reparar_doble_descuento_inventario.py --tenant <TENANT_ID> --aplicar

La reposición escribe un movimiento AJUSTE_DOBLE_DESCUENTO por item, así que la
corrección queda auditable y el script es idempotente: una vez reparado un item,
su ajuste queda registrado y no se vuelve a contar.

Requiere MONGO_USER / MONGO_PASSWORD / MONGO_HOST (o un .env en la raíz).
"""
import os
import sys
import argparse
from collections import defaultdict
from datetime import datetime, timezone

from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv()

CONCEPTO_AJUSTE = "AJUSTE_DOBLE_DESCUENTO"
# Conceptos con los que el toggle "entregado" de la OS descontaba stock.
CONCEPTOS_OS = ["CONSUMO", "CONSUMO_OS"]


def _client() -> MongoClient:
    user = os.environ.get("MONGO_USER")
    password = os.environ.get("MONGO_PASSWORD")
    host = os.environ.get("MONGO_HOST")
    db_name = os.environ.get("MONGO_DB", "siga")
    if not (user and password and host):
        print("[ERROR] Faltan MONGO_USER / MONGO_PASSWORD / MONGO_HOST (o .env).")
        sys.exit(1)
    uri = f"mongodb+srv://{user}:{password}@{host}/{db_name}?retryWrites=true&w=majority"
    return MongoClient(uri)


def _tenant_db(client, tenant_id):
    return client[f"t_{tenant_id.replace('-', '')}"]


def _listar_tenants(client):
    try:
        talleres = list(client["_platform"]["talleres"].find(
            {}, {"tenantId": 1, "nombreComercial": 1}))
        return [(t.get("tenantId"), t.get("nombreComercial", "?"))
                for t in talleres if t.get("tenantId")]
    except Exception as e:
        print(f"[WARN] No se pudo leer _platform.talleres: {e}")
        return []


def _ya_reparados(db):
    """item_id -> piezas ya repuestas por una corrida previa de este script."""
    repuesto = defaultdict(int)
    for mov in db["inventario_movimientos"].find({"concepto": CONCEPTO_AJUSTE}):
        if mov.get("item_id"):
            repuesto[mov["item_id"]] += int(mov.get("cantidad", 0) or 0)
    return repuesto


def detectar(db):
    """Devuelve (duplicados_por_item, detalle_por_os).

    duplicados_por_item: item_id -> piezas descontadas de más (ya netas de
    reparaciones previas).
    detalle_por_os: lista de dicts para el reporte en pantalla.
    """
    # Ventas ligadas a una OS: venta_id -> orden_id.
    ventas = {}
    for v in db["ventas"].find({"orden_id": {"$exists": True, "$ne": None}},
                               {"orden_id": 1, "folio": 1}):
        ventas[str(v["_id"])] = {"orden_id": v["orden_id"], "folio": v.get("folio", "?")}

    # Salidas por venta, agrupadas por (orden, item).
    salidas_venta = defaultdict(int)
    for mov in db["inventario_movimientos"].find({"concepto": "VENTA"}):
        venta = ventas.get(str(mov.get("referencia_id")))
        if not venta or not mov.get("item_id"):
            continue
        salidas_venta[(venta["orden_id"], mov["item_id"])] += abs(int(mov.get("cantidad", 0) or 0))

    # Salidas por la OS (toggle "entregado").
    salidas_os = defaultdict(int)
    nombres = {}
    for mov in db["inventario_movimientos"].find({"concepto": {"$in": CONCEPTOS_OS}}):
        orden_id = mov.get("referencia_id")
        item_id = mov.get("item_id")
        if not orden_id or not item_id:
            continue
        cantidad = int(mov.get("cantidad", 0) or 0)
        if cantidad >= 0:  # las devoluciones no cuentan como salida
            continue
        salidas_os[(orden_id, item_id)] += abs(cantidad)
        nombres[item_id] = mov.get("item_nombre") or nombres.get(item_id, "?")

    repuesto = _ya_reparados(db)
    duplicados = defaultdict(int)
    detalle = []
    for (orden_id, item_id), piezas_os in salidas_os.items():
        piezas_venta = salidas_venta.get((orden_id, item_id), 0)
        dobles = min(piezas_os, piezas_venta)
        if dobles <= 0:
            continue
        duplicados[item_id] += dobles
        orden = db["ordenes_servicio"].find_one({"_id": _oid(orden_id)}, {"folio": 1}) or {}
        detalle.append({
            "orden_id": orden_id,
            "folio": orden.get("folio", "?"),
            "item_id": item_id,
            "item_nombre": nombres.get(item_id, "?"),
            "piezas_os": piezas_os,
            "piezas_venta": piezas_venta,
            "duplicadas": dobles,
        })

    # Restar lo que una corrida previa ya repuso.
    for item_id in list(duplicados):
        neto = duplicados[item_id] - repuesto.get(item_id, 0)
        if neto <= 0:
            del duplicados[item_id]
        else:
            duplicados[item_id] = neto

    detalle.sort(key=lambda d: d["folio"])
    return duplicados, detalle


def _oid(valor):
    from bson import ObjectId
    from bson.errors import InvalidId
    try:
        return ObjectId(valor)
    except (InvalidId, TypeError):
        return None


def reparar(db, duplicados, etiqueta):
    """Repone el stock duplicado y registra el ajuste en el kardex."""
    ahora = datetime.now(timezone.utc)
    aplicados = 0
    for item_id, piezas in duplicados.items():
        oid = _oid(item_id)
        if not oid:
            print(f"     [SKIP] item_id inválido: {item_id}")
            continue
        item = db["items"].find_one({"_id": oid}, {"stock": 1, "nombre": 1, "sucursal_id": 1})
        if not item:
            print(f"     [SKIP] item {item_id} ya no existe.")
            continue
        stock_anterior = int(item.get("stock", 0) or 0)
        db["items"].update_one({"_id": oid}, {"$inc": {"stock": piezas}})
        db["inventario_movimientos"].insert_one({
            "item_id": item_id,
            "item_nombre": item.get("nombre"),
            "sucursal_id": item.get("sucursal_id"),
            "cantidad": piezas,
            "stock_anterior": stock_anterior,
            "stock_resultante": stock_anterior + piezas,
            "concepto": CONCEPTO_AJUSTE,
            "motivo": "Reposición de piezas descontadas dos veces (OS + POS) antes del fix.",
            "usuario_nombre": "script/reparar_doble_descuento_inventario",
            "createdAt": ahora,
        })
        aplicados += 1
        print(f"     [OK] {item.get('nombre', '?'):<35} {stock_anterior} -> {stock_anterior + piezas}")
    print(f"     [OK] {etiqueta}: {aplicados} item(s) repuestos.")
    return aplicados


def revisar_tenant(db, etiqueta, aplicar=False):
    duplicados, detalle = detectar(db)
    if not detalle:
        print(f"  [OK] {etiqueta}: sin dobles descuentos.")
        return 0

    print(f"  [!!] {etiqueta}: {len(detalle)} línea(s) con doble descuento:")
    for d in detalle:
        print(f"     - {d['folio']:<14} {d['item_nombre']:<32} "
              f"OS:{d['piezas_os']} + Venta:{d['piezas_venta']} => sobran {d['duplicadas']}")
    total_piezas = sum(duplicados.values())
    if not duplicados:
        print("     Todas las diferencias ya fueron repuestas por una corrida previa.")
        return 0
    print(f"     PIEZAS A REPONER: {total_piezas} en {len(duplicados)} item(s)")

    if aplicar:
        reparar(db, duplicados, etiqueta)
    else:
        print("     (simulación — vuelve a correr con --aplicar para reponer el stock)")
    return len(detalle)


def main():
    parser = argparse.ArgumentParser(
        description="Detecta y repone inventario descontado dos veces (OS + POS).")
    grupo = parser.add_mutually_exclusive_group(required=True)
    grupo.add_argument("--tenant", help="Tenant ID concreto a revisar.")
    grupo.add_argument("--todos", action="store_true", help="Revisar todos los tenants.")
    parser.add_argument("--aplicar", action="store_true",
                        help="Repone el stock. Sin este flag sólo mide.")
    args = parser.parse_args()

    client = _client()
    print("=" * 78)
    print("DOBLE DESCUENTO DE INVENTARIO (OS + POS)" +
          ("  [APLICANDO]" if args.aplicar else "  [SIMULACIÓN]"))
    print("=" * 78)

    total = 0
    if args.tenant:
        total += revisar_tenant(_tenant_db(client, args.tenant), args.tenant, args.aplicar)
    else:
        for tenant_id, nombre in _listar_tenants(client):
            total += revisar_tenant(
                _tenant_db(client, tenant_id), f"{nombre} ({tenant_id})", args.aplicar)

    print("-" * 78)
    print(f"Líneas afectadas en total: {total}")
    if total and not args.aplicar:
        print("Corre de nuevo con --aplicar para reponer el stock.")


if __name__ == "__main__":
    main()
