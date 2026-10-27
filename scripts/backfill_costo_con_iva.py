"""Backfill: costo promedio y costo de venta guardados SIN IVA (antes del fix 2026-10-27).

La operación va sin IVA (el ingreso de una venta es el `total` cobrado), pero al
recibir una compra `compras_manager` recalculaba `items.costo_promedio` con el costo
neto (÷1.16). La venta toma ese costo como `costo_unitario_snapshot`, así que la
utilidad salía inflada: acumulador comprado en $2,639 neto se costeaba en $2,275.

Por tenant:

1. Ventas: cada línea cuyo `costo_unitario_snapshot` coincide con el costo neto de
   una línea de compra recibida de ese mismo artículo (por item_id o no_parte) pasa
   al costo bruto de esa compra (lo que se le pagó al proveedor).
2. Artículos: si `costo_promedio` ≈ `precio_compra` / 1.16 (la última compra fijó el
   promedio), pasa a `precio_compra`. Los promedios mezclados se listan como
   [REVISAR] y no se tocan.

Cada documento modificado guarda el valor anterior en `_backfill_costo_iva`.
Dry-run por default; `--apply` escribe. Requiere MONGO_USER / MONGO_PASSWORD / MONGO_HOST.

    python scripts/backfill_costo_con_iva.py [--tenant t_xxx] [--apply]
"""
import argparse
import os
from datetime import datetime
from urllib.parse import quote_plus

from pymongo import MongoClient

TOL = 0.02


def _costos_compra(db):
    """{clave: {neto_redondeado: bruto}} por item_id y por no_parte, de compras no canceladas.

    Sólo compras capturadas a mano: las que nacen de una venta (VENTA_OS, COSTEO_VENTA)
    toman el costo de la venta como neto, así que coincidirían con él sin estar mal."""
    por_clave = {}
    filtro = {"estado": {"$ne": "CANCELADA"}, "origen": {"$nin": ["VENTA_OS", "COSTEO_VENTA"]}}
    for c in db.compras.find(filtro, {"items": 1}):
        for ln in c.get("items") or []:
            cant = int(ln.get("cantidad") or 0)
            neto = float(ln.get("costo_unitario_neto") or 0)
            if cant <= 0 or neto <= 0:
                continue
            bruto = round(float(ln.get("total_linea") or 0) / cant, 2)
            if abs(bruto - neto) <= TOL:
                continue  # exento: no hubo diferencia
            for clave in (ln.get("item_id"), ln.get("no_parte")):
                if clave and clave != "manual":
                    por_clave.setdefault(str(clave), {})[round(neto, 2)] = bruto
    return por_clave


def _bruto_para(costos, claves, snapshot):
    for clave in claves:
        for neto, bruto in costos.get(str(clave), {}).items():
            if abs(neto - snapshot) <= TOL:
                return bruto
    return None


def procesar_ventas(db, costos, apply):
    n_lineas = 0
    delta = 0.0
    for v in db.ventas.find({"items.costo_unitario_snapshot": {"$gt": 0}}, {"folio": 1, "items": 1}):
        items = v.get("items") or []
        cambios = {}
        for i, it in enumerate(items):
            snap = float(it.get("costo_unitario_snapshot") or 0)
            if snap <= 0:
                continue
            prod = it.get("producto") or {}
            claves = [it.get("item_id"), it.get("producto_id"), prod.get("_id"), prod.get("id"),
                      it.get("no_parte"), prod.get("no_parte"), prod.get("noParte")]
            bruto = _bruto_para(costos, [c for c in claves if c], snap)
            if bruto is None or bruto <= snap:
                continue
            cant = float(it.get("cantidad") or 1)
            cambios[i] = (snap, bruto)
            delta += (bruto - snap) * cant
            nombre = prod.get("nombre") or it.get("nombre") or "?"
            print(f"  [VENTA] {v.get('folio')} · {nombre[:40]}: {snap:,.2f} -> {bruto:,.2f} x{cant:g}")
        if cambios:
            n_lineas += len(cambios)
            if apply:
                sets = {f"items.{i}.costo_unitario_snapshot": b for i, (_, b) in cambios.items()}
                sets["_backfill_costo_iva"] = {
                    "fecha": datetime.utcnow(),
                    "anterior": {str(i): s for i, (s, _) in cambios.items()},
                }
                db.ventas.update_one({"_id": v["_id"]}, {"$set": sets})
    print(f"  ventas: {n_lineas} líneas, utilidad baja ${delta:,.2f}")


def procesar_items(db, apply):
    corregidos = revisar = 0
    for it in db.items.find({"ultima_compra_folio": {"$exists": True}},
                            {"nombre": 1, "no_parte": 1, "costo_promedio": 1, "precio_compra": 1, "stock": 1}):
        prom = float(it.get("costo_promedio") or 0)
        pc = float(it.get("precio_compra") or 0)
        if prom <= 0 or pc <= 0 or abs(prom - pc) <= TOL:
            continue
        etiqueta = f"{(it.get('nombre') or '?')[:40]} ({it.get('no_parte') or '-'})"
        if abs(prom - pc / 1.16) <= TOL:
            corregidos += 1
            print(f"  [ITEM] {etiqueta}: promedio {prom:,.2f} -> {pc:,.2f}")
            if apply:
                db.items.update_one({"_id": it["_id"]}, {"$set": {
                    "costo_promedio": pc,
                    "_backfill_costo_iva": {"fecha": datetime.utcnow(), "anterior": prom},
                }})
        else:
            revisar += 1
            print(f"  [REVISAR] {etiqueta}: promedio {prom:,.2f} vs última compra {pc:,.2f} (stock {it.get('stock')})")
    print(f"  items: {corregidos} corregidos, {revisar} a revisar")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tenant", help="limitar a una base t_xxx")
    ap.add_argument("--apply", action="store_true", help="escribir (default: dry-run)")
    args = ap.parse_args()

    user, pwd, host = (os.environ[k] for k in ("MONGO_USER", "MONGO_PASSWORD", "MONGO_HOST"))
    client = MongoClient(f"mongodb+srv://{quote_plus(user)}:{quote_plus(pwd)}@{host}/?retryWrites=true")
    tenants = [args.tenant] if args.tenant else [
        n for n in client.list_database_names() if n.startswith("t_")
    ]
    print("MODO:", "APPLY" if args.apply else "DRY-RUN")
    for t in tenants:
        db = client[t]
        if not db.compras.estimated_document_count():
            continue
        print(f"\n== {t}")
        procesar_ventas(db, _costos_compra(db), args.apply)
        procesar_items(db, args.apply)


if __name__ == "__main__":
    main()
