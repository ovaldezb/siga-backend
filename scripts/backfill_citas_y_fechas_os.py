"""
backfill_citas_y_fechas_os.py — Backoffice de reconciliación para TODOS los tenants.

Arregla dos backlogs históricos generados antes de los fixes de 2026-06-27:

  1) CITAS clavadas en "en_proceso": al convertir una cita en OS, la cita quedaba
     con orden_id + estado "en_proceso" y nada la regresaba a "completada" cuando
     su OS se finalizaba/entregaba/cancelaba. Aquí la sincronizamos:
         OS FINALIZADO/ENTREGADO -> cita "completada"
         OS CANCELADO            -> cita "cancelada"
     (Nunca pisa citas ya completada/cancelada.)

  2) FECHAS DE CIERRE de OS: el POS cerraba la OS con $set del estado pero no
     registraba la transición en bitacora_estados, así que "F. Finalización" /
     "F. Entrega" quedaban vacías. Aquí empujamos las entradas faltantes usando
     la mejor fecha disponible (pago_info.fecha -> venta.createdAt -> updatedAt
     -> createdAt). Marca cada entrada con backfill=True para trazabilidad.

Idempotente: re-correrlo no duplica nada.

Uso:
  # DRY-RUN (no escribe nada, solo reporta) sobre todos los tenants:
  python scripts/backfill_citas_y_fechas_os.py --todos

  # Aplicar de verdad:
  python scripts/backfill_citas_y_fechas_os.py --todos --apply

  # Un solo tenant:
  python scripts/backfill_citas_y_fechas_os.py --tenant <TENANT_ID> [--apply]

Requiere MONGO_USER / MONGO_PASSWORD / MONGO_HOST (o .env en la raíz del repo).
"""
import os
import sys
import argparse
from datetime import datetime, timezone
from bson import ObjectId
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv()

OS_TERMINALES = ["FINALIZADO", "ENTREGADO", "CANCELADO"]
OS_ESTADO_A_CITA = {"FINALIZADO": "completada", "ENTREGADO": "completada", "CANCELADO": "cancelada"}


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
        talleres = list(client["_platform"]["talleres"].find({}, {"tenantId": 1, "nombreComercial": 1}))
        return [(t.get("tenantId"), t.get("nombreComercial", "?")) for t in talleres if t.get("tenantId")]
    except Exception as e:
        print(f"[WARN] No se pudo leer _platform.talleres: {e}")
        return []


def _to_iso(value):
    """Normaliza un valor de fecha (datetime o str ISO) a string ISO con sufijo Z."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.isoformat() + "Z"
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "") + "Z"
    if isinstance(value, str) and value.strip():
        return value
    return None


# ---------------------------------------------------------------------------
# 1) Sincronización de citas
# ---------------------------------------------------------------------------
def sync_citas(db, apply):
    citas = list(db["citas"].find(
        {"orden_id": {"$nin": [None, ""]}, "estado": {"$nin": ["completada", "cancelada"]}},
        {"orden_id": 1, "estado": 1, "clienteNombre": 1}
    ))
    if not citas:
        return 0

    oids = []
    for c in citas:
        try:
            oids.append(ObjectId(c["orden_id"]))
        except Exception:
            pass
    os_map = {}
    if oids:
        for o in db["ordenes_servicio"].find({"_id": {"$in": oids}}, {"estado": 1}):
            os_map[str(o["_id"])] = o.get("estado")

    cambios = 0
    for c in citas:
        os_estado = os_map.get(str(c.get("orden_id")))
        nuevo = OS_ESTADO_A_CITA.get(os_estado)
        if nuevo and nuevo != c.get("estado"):
            cambios += 1
            print(f"       cita {c['_id']} ({c.get('clienteNombre','?')}): "
                  f"{c.get('estado')} -> {nuevo}  [OS {os_estado}]")
            if apply:
                db["citas"].update_one(
                    {"_id": c["_id"]},
                    {"$set": {"estado": nuevo, "updatedAt": _to_iso(datetime.now(timezone.utc))}}
                )
    return cambios


# ---------------------------------------------------------------------------
# 2) Backfill de fechas de cierre (bitacora_estados) en OS
# ---------------------------------------------------------------------------
def backfill_fechas_os(db, apply):
    ordenes = list(db["ordenes_servicio"].find(
        {"estado": {"$in": ["FINALIZADO", "ENTREGADO"]}},
        {"estado": 1, "bitacora_estados": 1, "pago_info": 1, "updatedAt": 1, "createdAt": 1}
    ))
    if not ordenes:
        return 0

    cambios = 0
    for o in ordenes:
        estados_presentes = {b.get("estado") for b in (o.get("bitacora_estados") or [])}
        falta_fin = "FINALIZADO" not in estados_presentes
        falta_ent = (o.get("estado") == "ENTREGADO") and ("ENTREGADO" not in estados_presentes)
        if not falta_fin and not falta_ent:
            continue

        # Mejor fecha disponible para representar el cierre.
        fecha = _to_iso((o.get("pago_info") or {}).get("fecha"))
        if not fecha:
            venta = db["ventas"].find_one({"orden_id": str(o["_id"])}, {"createdAt": 1})
            if venta:
                fecha = _to_iso(venta.get("createdAt"))
        if not fecha:
            fecha = _to_iso(o.get("updatedAt")) or _to_iso(o.get("createdAt"))
        if not fecha:
            continue  # sin ninguna fecha de referencia, no inventamos

        nuevas = []
        if falta_fin:
            nuevas.append({"estado": "FINALIZADO", "fecha": fecha, "usuario_id": "system:backfill", "backfill": True})
        if falta_ent:
            nuevas.append({"estado": "ENTREGADO", "fecha": fecha, "usuario_id": "system:backfill", "backfill": True})

        cambios += 1
        etiquetas = ", ".join(n["estado"] for n in nuevas)
        print(f"       OS {o['_id']} [{o.get('estado')}]: + {etiquetas} @ {fecha}")
        if apply:
            db["ordenes_servicio"].update_one(
                {"_id": o["_id"]},
                {"$push": {"bitacora_estados": {"$each": nuevas}}}
            )
    return cambios


def procesar_tenant(db, etiqueta, apply):
    print(f"\n  >> {etiqueta}")
    c = sync_citas(db, apply)
    f = backfill_fechas_os(db, apply)
    if c == 0 and f == 0:
        print("     [OK] nada pendiente.")
    else:
        print(f"     citas={c}  fechas_os={f}")
    return c, f


def main():
    parser = argparse.ArgumentParser(description="Backfill citas + fechas de cierre OS para todos los tenants.")
    grupo = parser.add_mutually_exclusive_group(required=True)
    grupo.add_argument("--tenant", help="Tenant ID concreto.")
    grupo.add_argument("--todos", action="store_true", help="Todos los tenants de _platform.talleres.")
    parser.add_argument("--apply", action="store_true", help="Aplicar cambios (sin esto es dry-run).")
    args = parser.parse_args()

    modo = "APPLY (escribiendo)" if args.apply else "DRY-RUN (solo lectura)"
    client = _client()
    print(f"Conectado a MongoDB. Modo: {modo}")

    tot_c = tot_f = 0
    if args.todos:
        tenants = _listar_tenants(client)
        if not tenants:
            print("No se encontraron tenants en _platform.talleres.")
            sys.exit(1)
        for tid, nombre in tenants:
            c, f = procesar_tenant(_tenant_db(client, tid), f"{nombre} ({tid})", args.apply)
            tot_c += c
            tot_f += f
    else:
        c, f = procesar_tenant(_tenant_db(client, args.tenant), args.tenant, args.apply)
        tot_c += c
        tot_f += f

    print(f"\n=== TOTAL: citas={tot_c}  fechas_os={tot_f} ===")
    if not args.apply and (tot_c or tot_f):
        print("Esto fue DRY-RUN. Re-corre con --apply para aplicar los cambios.")


if __name__ == "__main__":
    main()
