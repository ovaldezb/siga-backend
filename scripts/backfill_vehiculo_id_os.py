"""Backfill: OS sin `vehiculo_id` (convertidas de cotización antes del fix 2026-10-19).

`convertir_a_os_handler` copiaba `vehiculo_snapshot` pero no `vehiculo_id`, y todo el
historial del vehículo (rechazos a re-ofrecer, km, próximos cambios) se busca por ese
campo. Este script, por tenant:

1. Liga cada OS sin `vehiculo_id` a `vehiculo_snapshot.id` si ese vehículo existe.
2. Para cada vehículo tocado, si no tiene próximos cambios guardados, toma los de
   su OS más reciente que los tenga (lo que habría hecho `_sync_mantenimiento_to_vehiculo`).
3. Para las OS abiertas de esos vehículos (no ENTREGADO/CANCELADO) sin ningún
   `*_anterior`, los llena con los próximos cambios de la OS previa del vehículo.

Dry-run por default; `--apply` escribe. Requiere MONGO_USER / MONGO_PASSWORD / MONGO_HOST.

    python scripts/backfill_vehiculo_id_os.py [--tenant t_xxx] [--apply]
"""
import argparse
import os
from urllib.parse import quote_plus

from bson import ObjectId
from pymongo import MongoClient

SIN_VEHICULO = {"$or": [
    {"vehiculo_id": {"$exists": False}}, {"vehiculo_id": None},
    {"vehiculo_id": ""}, {"vehiculo_id": "None"},
]}
CAMPOS_MANT = ("proximo_cambio_aceite", "proximo_cambio_bujias",
               "proximo_cambio_aceite_fecha", "proximo_cambio_bujias_fecha")
CERRADAS = ("ENTREGADO", "CANCELADO")


def _tiene_valor(v) -> bool:
    if isinstance(v, str):
        return bool(v.strip())
    try:
        return float(v or 0) > 0
    except (TypeError, ValueError):
        return False


def _vehiculo_existe(db, vid) -> bool:
    try:
        return db.vehiculos.count_documents({"_id": ObjectId(str(vid))}, limit=1) > 0
    except Exception:
        return False


def procesar_tenant(db, apply: bool) -> None:
    tocados = set()
    for o in db.ordenes_servicio.find(SIN_VEHICULO, {"folio": 1, "vehiculo_snapshot.id": 1, "cotizacion_origen_id": 1}):
        vid = str((o.get("vehiculo_snapshot") or {}).get("id") or "")
        origen = "cotización" if o.get("cotizacion_origen_id") else "otro"
        if not vid or not _vehiculo_existe(db, vid):
            print(f"  [SKIP] {o.get('folio')} ({origen}): snapshot sin vehículo válido ({vid or '-'})")
            continue
        print(f"  [LIGA] {o.get('folio')} ({origen}) -> vehiculo {vid}")
        tocados.add(vid)
        if apply:
            db.ordenes_servicio.update_one({"_id": o["_id"]}, {"$set": {"vehiculo_id": vid}})

    for vid in tocados:
        veh = db.vehiculos.find_one({"_id": ObjectId(vid)}, {c: 1 for c in CAMPOS_MANT})
        # Las OS ya ligadas (o que se ligarán con --apply) de este vehículo, viejas primero.
        filtro = {"$or": [{"vehiculo_id": vid}, {"vehiculo_snapshot.id": vid, **SIN_VEHICULO}]}
        ordenes = list(db.ordenes_servicio.find(filtro).sort("createdAt", 1))

        faltan = {c for c in CAMPOS_MANT if not _tiene_valor(veh.get(c))}
        set_veh = {}
        for o in reversed(ordenes):
            for c in list(faltan):
                if _tiene_valor(o.get(c)):
                    set_veh[c] = float(o[c]) if not isinstance(o[c], str) else o[c]
                    faltan.discard(c)
        if set_veh:
            print(f"  [VEH ] {vid}: {set_veh}")
            if apply:
                db.vehiculos.update_one({"_id": ObjectId(vid)}, {"$set": set_veh})

        for i, o in enumerate(ordenes):
            if i == 0 or o.get("estado") in CERRADAS:
                continue
            if any(_tiene_valor(o.get(f"{c}_anterior")) for c in CAMPOS_MANT):
                continue
            previa = ordenes[i - 1]
            set_os = {f"{c}_anterior": previa.get(c) for c in CAMPOS_MANT if _tiene_valor(previa.get(c))}
            if set_os:
                print(f"  [ANT ] {o.get('folio')} <- {previa.get('folio')}: {set_os}")
                if apply:
                    db.ordenes_servicio.update_one({"_id": o["_id"]}, {"$set": set_os})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", help="DB del tenant (t_xxx); por default todos")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    user, pwd, host = (os.environ.get(k) for k in ("MONGO_USER", "MONGO_PASSWORD", "MONGO_HOST"))
    if not (user and pwd and host):
        raise SystemExit("Faltan MONGO_USER / MONGO_PASSWORD / MONGO_HOST")
    client = MongoClient(f"mongodb+srv://{quote_plus(user)}:{quote_plus(pwd)}@{host}/?retryWrites=true")

    print("MODO:", "APPLY" if args.apply else "DRY-RUN")
    for dbn in ([args.tenant] if args.tenant else client.list_database_names()):
        if not dbn.startswith("t_"):
            continue
        db = client[dbn]
        if db.ordenes_servicio.count_documents(SIN_VEHICULO, limit=1) == 0:
            continue
        print(f"== {dbn}")
        procesar_tenant(db, args.apply)


if __name__ == "__main__":
    main()
