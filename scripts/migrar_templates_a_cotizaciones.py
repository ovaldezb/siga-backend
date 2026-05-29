"""
migrar_templates_a_cotizaciones.py -- Mueve `configuracion.templates_revision[]`
(array dentro del doc de configuracion por tenant) a la nueva coleccion
`cotizaciones` con `tipo='PLANTILLA'`.

Idempotente: si ya existe una cotización tipo=PLANTILLA con el mismo `nombre`
y `tenant_id`, se omite.

NO borra el array de templates_revision — la lectura legacy sigue funcionando
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

    # Vaciar config.templates_revision tras validación
    python scripts/migrar_templates_a_cotizaciones.py --tenant <ID> --vaciar-config
"""
import argparse
import os
import sys
from datetime import datetime

from dotenv import load_dotenv
from pymongo import ReturnDocument

# Asegura import desde la raíz del proyecto
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

load_dotenv()

from src.shared.infrastructure.database import get_platform_db, get_tenant_db  # noqa: E402


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
    """Suma simple para plantillas — no aplica IVA distintos por línea."""
    total = 0.0
    for p in puntos or []:
        for it in (p.get("items") or []):
            try:
                total += float(it.get("piezas") or 0) * float(it.get("precioVenta") or 0)
            except (TypeError, ValueError):
                continue
    return round(total, 2)


def _normalize_items(items):
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        out.append({
            "nombre": (it.get("nombre") or "").strip() or "Sin nombre",
            "piezas": float(it.get("piezas") or 1),
            "precioVenta": float(it.get("precioVenta") or 0),
            "precioCompra": float(it.get("precioCompra") or 0),
            "subtotal": float(it.get("piezas") or 1) * float(it.get("precioVenta") or 0),
            "aprobado": False,
            "entregado": False,
            "tipo": (it.get("tipo") or "PRODUCTO"),
        })
    return out


def _normalize_puntos(puntos):
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
                "items": _normalize_items(p.get("items")),
            })
    return out


def migrar_tenant(tenant_id: str, dry_run: bool = False, vaciar_config: bool = False) -> dict:
    db = get_tenant_db(tenant_id)
    config = db["configuracion"].find_one({"tenant_id": tenant_id}, {"templates_revision": 1})
    templates = (config or {}).get("templates_revision") or []

    insertados = 0
    omitidos = 0
    plan = []

    # Set de nombres ya migrados (idempotencia)
    ya_migrados = set(
        (c.get("nombre") or "").strip().lower()
        for c in db["cotizaciones"].find(
            {"tenant_id": tenant_id, "tipo": "PLANTILLA"},
            {"nombre": 1},
        )
    )

    for tpl in templates:
        nombre = (tpl.get("nombre") or "").strip()
        if not nombre:
            omitidos += 1
            continue
        if nombre.lower() in ya_migrados:
            omitidos += 1
            plan.append(("SKIP (ya migrada)", nombre, 0))
            continue

        puntos = _normalize_puntos(tpl.get("puntos"))
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
    args = parser.parse_args()

    if args.todos:
        plat = get_platform_db()
        tenants = [t["tenantId"] for t in plat.talleres.find({}, {"tenantId": 1, "_id": 0}) if t.get("tenantId")]
    else:
        tenants = [args.tenant]

    print("# Migrar templates_revision -> cotizaciones (tipo=PLANTILLA)")
    print(f"# Tenants a procesar: {len(tenants)} -- dry-run={args.dry_run} -- vaciar-config={args.vaciar_config}")

    totales = {"insertados": 0, "omitidos": 0, "leidos": 0}
    for tid in tenants:
        try:
            r = migrar_tenant(tid, dry_run=args.dry_run, vaciar_config=args.vaciar_config)
            print(f"\n[{tid}]")
            print(f"  leídos={r['leidos_config']}  insertados={r['insertados_cotizaciones']}  omitidos={r['omitidos']}")
            for accion, nombre, total in r["plan"][:30]:
                print(f"    {accion:20s} {nombre[:50]:50s}  $ {total:>10.2f}")
            if len(r["plan"]) > 30:
                print(f"    … +{len(r['plan']) - 30} más")
            totales["insertados"] += r["insertados_cotizaciones"]
            totales["omitidos"] += r["omitidos"]
            totales["leidos"] += r["leidos_config"]
        except Exception as e:
            print(f"\n[{tid}] ERROR: {e}")

    print(f"\n# TOTAL: leídos={totales['leidos']}  insertados={totales['insertados']}  omitidos={totales['omitidos']}")
    if args.dry_run:
        print("# (dry-run — no se escribió nada)")


if __name__ == "__main__":
    main()
