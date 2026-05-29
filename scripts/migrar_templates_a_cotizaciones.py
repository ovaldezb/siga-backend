"""
migrar_templates_a_cotizaciones.py -- Mueve `configuracion.templates_revision[]`
(array dentro del doc de configuracion por tenant) a la nueva coleccion
`cotizaciones` con `tipo='PLANTILLA'`.

Idempotente: si ya existe una cotización tipo=PLANTILLA con el mismo `nombre`
y `tenant_id`, se omite (salvo con --actualizar-precios, ver abajo).

Enriquecimiento de precios
--------------------------
El seed original (seed_templates_revision.py) sólo guardó NOMBRES de items
porque los precios varían entre OS. Como resultado, las plantillas migradas
salían con `precioVenta=0`.

Este script construye un índice {nombre_normalizado_item -> precio_más_reciente}
recorriendo TODAS las OS del tenant y, para cada item de la plantilla que no
traiga precio, usa el precio histórico más reciente encontrado. Si no hay
histórico, se queda en 0 (el admin lo teclea después).

NO borra el array de templates_revision -- la lectura legacy sigue funcionando
hasta que el frontend deje de leerlo. Para limpiar después de validar:

    python scripts/migrar_templates_a_cotizaciones.py --tenant <ID> --vaciar-config

Uso
---
    # Vista previa de un tenant
    python scripts/migrar_templates_a_cotizaciones.py --tenant <ID> --dry-run

    # Migrar realmente un tenant
    python scripts/migrar_templates_a_cotizaciones.py --tenant <ID>

    # Migrar TODOS los tenants
    python scripts/migrar_templates_a_cotizaciones.py --todos

    # Re-actualizar precios de plantillas YA migradas (rellena items en $0
    # con el precio histórico más reciente encontrado en las OS del tenant)
    python scripts/migrar_templates_a_cotizaciones.py --todos --actualizar-precios

    # Vaciar config.templates_revision tras validación
    python scripts/migrar_templates_a_cotizaciones.py --tenant <ID> --vaciar-config
"""
import argparse
import os
import re
import sys
from datetime import datetime

from dotenv import load_dotenv
from pymongo import ReturnDocument

# Asegura import desde la raíz del proyecto
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

load_dotenv()

from src.shared.infrastructure.database import get_platform_db, get_tenant_db  # noqa: E402


# ---------- normalización de nombres (mismo criterio que el seed) ------------

_WS = re.compile(r"\s+")


def _norm(s: str) -> str:
    if not s:
        return ""
    s = s.strip().lower()
    s = _WS.sub(" ", s)
    return s.translate(str.maketrans("áéíóúüñ", "aeiouun"))


# ---------- índice de precios desde OS históricas -----------------------------

def _construir_indice_precios(db) -> dict:
    """Recorre todas las OS del tenant y devuelve
       {nombre_normalizado_item -> precio_venta_más_reciente}.

    Sólo considera items con precioVenta > 0. Para items con varios precios
    históricos, se queda con el de la OS más reciente (por createdAt). Si la
    OS no tiene createdAt usa _id como fallback (ordenable por tiempo).
    """
    indice: dict = {}  # nombre_norm -> (fecha_orden, precio)
    cursor = db["ordenes_servicio"].find(
        {"puntosArreglar": {"$exists": True, "$ne": []}},
        {"puntosArreglar": 1, "createdAt": 1, "_id": 1},
    )
    for orden in cursor:
        fecha = orden.get("createdAt") or orden.get("_id")
        for punto in orden.get("puntosArreglar") or []:
            for it in (punto or {}).get("items") or []:
                nombre = (it.get("nombre") or "").strip()
                if not nombre:
                    continue
                try:
                    precio = float(it.get("precioVenta") or 0)
                except (TypeError, ValueError):
                    continue
                if precio <= 0:
                    continue
                norm = _norm(nombre)
                if not norm:
                    continue
                prev = indice.get(norm)
                if prev is None:
                    indice[norm] = (fecha, precio)
                else:
                    prev_fecha, _ = prev
                    if fecha and (prev_fecha is None or fecha > prev_fecha):
                        indice[norm] = (fecha, precio)
    return {k: v[1] for k, v in indice.items()}


def _next_folio_plantilla(db) -> str:
    res = db.folios.find_one_and_update(
        {"tipo": "tpl", "sucursal_id": "*"},
        {"$inc": {"secuencia": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    seq = res.get("secuencia", 1)
    return f"TPL-{datetime.utcnow().strftime('%Y%m%d')}-{str(seq).zfill(4)}"


def _calcular_totales(puntos):
    """Suma simple para plantillas -- no aplica IVA distintos por línea."""
    total = 0.0
    for p in puntos or []:
        for it in (p.get("items") or []):
            try:
                total += float(it.get("piezas") or 0) * float(it.get("precioVenta") or 0)
            except (TypeError, ValueError):
                continue
    return round(total, 2)


def _normalize_items(items, indice_precios=None):
    out = []
    indice_precios = indice_precios or {}
    for it in items or []:
        if not isinstance(it, dict):
            continue
        nombre = (it.get("nombre") or "").strip() or "Sin nombre"
        try:
            precio_venta = float(it.get("precioVenta") or 0)
        except (TypeError, ValueError):
            precio_venta = 0.0
        if precio_venta <= 0:
            # Busca precio histórico por nombre normalizado.
            precio_hist = indice_precios.get(_norm(nombre))
            if precio_hist and precio_hist > 0:
                precio_venta = float(precio_hist)
        try:
            piezas = float(it.get("piezas") or 1)
        except (TypeError, ValueError):
            piezas = 1.0
        out.append({
            "nombre": nombre,
            "piezas": piezas,
            "precioVenta": precio_venta,
            "precioCompra": float(it.get("precioCompra") or 0),
            "subtotal": piezas * precio_venta,
            "aprobado": False,
            "entregado": False,
            "tipo": (it.get("tipo") or "PRODUCTO"),
        })
    return out


def _normalize_puntos(puntos, indice_precios=None):
    """Normaliza puntos de un template legacy. Soporta dos formas históricas:
       - v1: lista de strings (sólo nombres)
       - v2: lista de {nombre, items: [...]}
    """
    out = []
    for p in puntos or []:
        if isinstance(p, str):
            out.append({"nombre": p, "items": []})
        elif isinstance(p, dict):
            out.append({
                "nombre": (p.get("nombre") or "").strip() or "Sin nombre",
                "items": _normalize_items(p.get("items"), indice_precios=indice_precios),
            })
    return out


def _actualizar_precios_plantilla_existente(db, cotizacion_doc, indice_precios, dry_run=False):
    """Para una plantilla YA migrada, rellena precios de items en $0 usando
    el índice histórico. Devuelve (n_items_actualizados, nuevo_total)."""
    puntos = cotizacion_doc.get("puntosArreglar") or []
    actualizados = 0
    cambio = False
    for punto in puntos:
        for it in punto.get("items") or []:
            try:
                precio_actual = float(it.get("precioVenta") or 0)
            except (TypeError, ValueError):
                precio_actual = 0.0
            if precio_actual > 0:
                continue
            nombre = (it.get("nombre") or "").strip()
            if not nombre:
                continue
            precio_hist = indice_precios.get(_norm(nombre))
            if precio_hist and precio_hist > 0:
                it["precioVenta"] = float(precio_hist)
                try:
                    piezas = float(it.get("piezas") or 1)
                except (TypeError, ValueError):
                    piezas = 1.0
                it["piezas"] = piezas
                it["subtotal"] = piezas * float(precio_hist)
                actualizados += 1
                cambio = True

    if not cambio:
        return 0, float(cotizacion_doc.get("total") or 0)

    nuevo_total = _calcular_totales(puntos)
    if not dry_run:
        db["cotizaciones"].update_one(
            {"_id": cotizacion_doc["_id"]},
            {"$set": {
                "puntosArreglar": puntos,
                "subtotal": nuevo_total,
                "total": nuevo_total,
                "updatedAt": datetime.utcnow(),
                "_precios_enriquecidos_at": datetime.utcnow().isoformat() + "Z",
            }},
        )
    return actualizados, nuevo_total


def migrar_tenant(tenant_id: str, dry_run: bool = False, vaciar_config: bool = False,
                  actualizar_precios: bool = False) -> dict:
    db = get_tenant_db(tenant_id)

    # Índice de precios desde OS históricas del tenant.
    indice_precios = _construir_indice_precios(db)

    config = db["configuracion"].find_one({"tenant_id": tenant_id}, {"templates_revision": 1})
    templates = (config or {}).get("templates_revision") or []

    insertados = 0
    omitidos = 0
    plan = []

    # Plantillas tipo=PLANTILLA ya migradas (por nombre normalizado).
    ya_migradas_por_nombre = {
        (c.get("nombre") or "").strip().lower(): c
        for c in db["cotizaciones"].find(
            {"tenant_id": tenant_id, "tipo": "PLANTILLA"},
            {"nombre": 1, "puntosArreglar": 1, "total": 1, "_id": 1},
        )
    }

    # 1) Re-actualizar precios en plantillas ya migradas, si se pidió.
    precios_actualizados_total = 0
    plantillas_tocadas = 0
    if actualizar_precios:
        for nombre_lower, doc in ya_migradas_por_nombre.items():
            n_upd, nuevo_total = _actualizar_precios_plantilla_existente(
                db, doc, indice_precios, dry_run=dry_run,
            )
            if n_upd > 0:
                plantillas_tocadas += 1
                precios_actualizados_total += n_upd
                plan.append(("UPDATE-PRECIOS",
                             (doc.get("nombre") or "")[:50],
                             nuevo_total))

    # 2) Migrar templates_revision -> cotizaciones (insert si no existe).
    for tpl in templates:
        nombre = (tpl.get("nombre") or "").strip()
        if not nombre:
            omitidos += 1
            continue
        if nombre.lower() in ya_migradas_por_nombre:
            omitidos += 1
            plan.append(("SKIP (ya migrada)", nombre, 0))
            continue

        puntos = _normalize_puntos(tpl.get("puntos"), indice_precios=indice_precios)
        total = _calcular_totales(puntos)

        plan.append(("INSERT", nombre, total))
        if dry_run:
            insertados += 1
            continue

        now = datetime.utcnow()
        doc = {
            "tenant_id": tenant_id,
            "tipo": "PLANTILLA",
            "sucursal_id": None,
            "folio": _next_folio_plantilla(db),
            "nombre": nombre,
            "marca": None,
            "modelo": None,
            "anio_desde": None,
            "anio_hasta": None,
            "tipo_servicio": None,
            "cliente_snapshot": {},
            "vehiculo_snapshot": {},
            "puntosArreglar": puntos,
            "subtotal": total,
            "iva": 0,
            "total": total,
            "observaciones": "",
            "kilometraje": None,
            "createdAt": now,
            "updatedAt": now,
            "created_by": "migrar_templates_a_cotizaciones",
            "migrated_from": "configuracion.templates_revision",
        }
        db["cotizaciones"].insert_one(doc)
        insertados += 1

    if vaciar_config and not dry_run and insertados > 0:
        db["configuracion"].update_one(
            {"tenant_id": tenant_id},
            {"$set": {"templates_revision": [], "templates_revision_migrated_at": datetime.utcnow().isoformat() + "Z"}},
        )

    return {
        "tenant_id": tenant_id,
        "leidos_config": len(templates),
        "insertados_cotizaciones": insertados,
        "omitidos": omitidos,
        "indice_precios_size": len(indice_precios),
        "plantillas_tocadas": plantillas_tocadas,
        "precios_actualizados": precios_actualizados_total,
        "plan": plan,
        "vaciado_config": bool(vaciar_config and not dry_run and insertados > 0),
    }


def main():
    parser = argparse.ArgumentParser()
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--tenant", help="tenantId específico")
    g.add_argument("--todos", action="store_true", help="Procesar todos los tenants de _platform.talleres")
    parser.add_argument("--dry-run", action="store_true", help="Solo muestra el plan, no escribe")
    parser.add_argument(
        "--vaciar-config",
        action="store_true",
        help="Tras migrar OK, vacía configuracion.templates_revision (usar solo después de validar)",
    )
    parser.add_argument(
        "--actualizar-precios",
        action="store_true",
        help="Para plantillas YA migradas, rellena precios de items en $0 con el precio histórico más reciente.",
    )
    args = parser.parse_args()

    if args.todos:
        plat = get_platform_db()
        tenants = [t["tenantId"] for t in plat.talleres.find({}, {"tenantId": 1, "_id": 0}) if t.get("tenantId")]
    else:
        tenants = [args.tenant]

    print("# Migrar templates_revision -> cotizaciones (tipo=PLANTILLA)")
    print(f"# Tenants a procesar: {len(tenants)} -- dry-run={args.dry_run} -- "
          f"vaciar-config={args.vaciar_config} -- actualizar-precios={args.actualizar_precios}")

    totales = {"insertados": 0, "omitidos": 0, "leidos": 0,
               "plantillas_tocadas": 0, "precios_actualizados": 0}
    for tid in tenants:
        try:
            r = migrar_tenant(
                tid,
                dry_run=args.dry_run,
                vaciar_config=args.vaciar_config,
                actualizar_precios=args.actualizar_precios,
            )
            print(f"\n[{tid}]")
            print(f"  leídos={r['leidos_config']}  insertados={r['insertados_cotizaciones']}  "
                  f"omitidos={r['omitidos']}  índice_precios={r['indice_precios_size']}")
            if r["plantillas_tocadas"] or r["precios_actualizados"]:
                print(f"  precios_actualizados={r['precios_actualizados']} "
                      f"en {r['plantillas_tocadas']} plantilla(s) ya migradas")
            for accion, nombre, total in r["plan"][:30]:
                print(f"    {accion:20s} {nombre[:50]:50s}  $ {total:>10.2f}")
            if len(r["plan"]) > 30:
                print(f"    … +{len(r['plan']) - 30} más")
            totales["insertados"] += r["insertados_cotizaciones"]
            totales["omitidos"] += r["omitidos"]
            totales["leidos"] += r["leidos_config"]
            totales["plantillas_tocadas"] += r["plantillas_tocadas"]
            totales["precios_actualizados"] += r["precios_actualizados"]
        except Exception as e:
            print(f"\n[{tid}] ERROR: {e}")

    print(f"\n# TOTAL: leídos={totales['leidos']}  insertados={totales['insertados']}  "
          f"omitidos={totales['omitidos']}")
    if totales["precios_actualizados"]:
        print(f"# Precios actualizados: {totales['precios_actualizados']} item(s) "
              f"en {totales['plantillas_tocadas']} plantilla(s) ya migradas.")
    if args.dry_run:
        print("# (dry-run -- no se escribió nada)")


if __name__ == "__main__":
    main()
