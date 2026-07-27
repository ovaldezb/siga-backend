"""Alinea los documentos históricos a la operación SIN IVA (decisión 2026-07-24).

Contexto: desde el commit `fix(iva): operacion sin IVA en ventas, OS, cotizaciones y
contabilidad`, el precio capturado ES el monto a cobrar. Los documentos nuevos se
guardan con `subtotal == total` e `iva = 0`, pero los históricos traen el desglose
viejo (subtotal neto + iva aparte) o directamente no traen `subtotal`.

Qué hace, por cada doc de `ventas`, `ordenes_servicio` y `cotizaciones` de cada tenant:
    subtotal <- total        (el monto cobrado NO se toca)
    iva      <- 0.0
    _migracion_sin_iva <- respaldo de los valores anteriores

Qué NO hace:
- No toca `compras` (la factura del proveedor sí trae IVA acreditable real).
- No toca `total` en ningún documento: el ingreso histórico se conserva tal cual,
  incluidas las ventas viejas donde el total traía el 16% encima de las líneas.
- No toca los `subtotal` por línea (ya son brutos: precio * cantidad).
- No toca `precios_incluyen_iva` de la OS: quedó como metadato fiscal sin efecto.

Uso:
    python scripts/migrar_sin_iva_historico.py                 # dry-run (no escribe)
    python scripts/migrar_sin_iva_historico.py --apply         # aplica a todos los tenants
    python scripts/migrar_sin_iva_historico.py --apply --tenant t_45b55ae0...
    python scripts/migrar_sin_iva_historico.py --revertir --apply   # deshace usando el respaldo

Es idempotente: correrlo dos veces no vuelve a modificar nada ni pisa el respaldo original.
"""
import argparse
import os
import sys
from datetime import datetime, timezone

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from dotenv import load_dotenv

load_dotenv()

from pymongo import UpdateOne  # noqa: E402

from src.shared.infrastructure.database import MongoDBConnection  # noqa: E402

COLECCIONES = ("ventas", "ordenes_servicio", "cotizaciones")
CAMPO_RESPALDO = "_migracion_sin_iva"
TOL = 0.02


def f(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def nombres_tenants(client):
    """{db_name: nombre comercial} para etiquetar la salida."""
    mapa = {}
    for t in client["_platform"]["talleres"].find({}, {"tenantId": 1, "nombreComercial": 1}):
        tid = (t.get("tenantId") or "").replace("-", "")
        if tid:
            mapa[f"t_{tid}"] = t.get("nombreComercial") or "(sin nombre)"
    return mapa


def planear(db, coll_name, sello):
    """Devuelve (operaciones, sospechosos, ya_migrados) sin escribir nada."""
    coll = db[coll_name]
    ops, sospechosos, ya_ok = [], [], 0

    for doc in coll.find({}, {"subtotal": 1, "iva": 1, "total": 1, "folio": 1,
                              CAMPO_RESPALDO: 1}):
        total = f(doc.get("total"))
        subtotal = f(doc.get("subtotal"))
        iva = f(doc.get("iva"))
        tiene_subtotal = "subtotal" in doc
        tiene_iva = "iva" in doc

        desalineado = (
            abs(iva) > TOL
            or not tiene_subtotal
            or not tiene_iva
            or abs(subtotal - total) > TOL
        )
        if not desalineado:
            ya_ok += 1
            continue

        # Guarda: un doc con total 0 pero subtotal cargado significaría perder el
        # monto al copiar total->subtotal. No se toca; se reporta para revisión manual.
        if total <= TOL and subtotal > TOL:
            sospechosos.append((doc.get("folio") or str(doc["_id"]), subtotal, iva, total))
            continue

        cambios = {"subtotal": round(total, 2), "iva": 0.0}
        # El respaldo se escribe una sola vez: si el doc ya lo tiene (corrida previa),
        # se respeta el original para que --revertir siga siendo fiable.
        if CAMPO_RESPALDO not in doc:
            cambios[CAMPO_RESPALDO] = {
                "subtotal_anterior": doc.get("subtotal"),
                "iva_anterior": doc.get("iva"),
                "total_anterior": doc.get("total"),
                "fecha": sello,
                "script": "migrar_sin_iva_historico.py",
            }
        ops.append(UpdateOne({"_id": doc["_id"]}, {"$set": cambios}))

    return ops, sospechosos, ya_ok


def planear_reversa(db, coll_name):
    """Restaura subtotal/iva desde el respaldo. Solo docs que tengan el respaldo."""
    coll = db[coll_name]
    ops = []
    for doc in coll.find({CAMPO_RESPALDO: {"$exists": True}},
                         {CAMPO_RESPALDO: 1}):
        respaldo = doc[CAMPO_RESPALDO] or {}
        set_ops, unset_ops = {}, {CAMPO_RESPALDO: ""}
        if respaldo.get("subtotal_anterior") is None:
            unset_ops["subtotal"] = ""
        else:
            set_ops["subtotal"] = respaldo["subtotal_anterior"]
        if respaldo.get("iva_anterior") is None:
            unset_ops["iva"] = ""
        else:
            set_ops["iva"] = respaldo["iva_anterior"]
        update = {"$unset": unset_ops}
        if set_ops:
            update["$set"] = set_ops
        ops.append(UpdateOne({"_id": doc["_id"]}, update))
    return ops


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="escribe en la base (sin este flag es dry-run)")
    parser.add_argument("--tenant", help="limitar a una base t_xxx")
    parser.add_argument("--revertir", action="store_true",
                        help="deshace la migración usando el respaldo _migracion_sin_iva")
    args = parser.parse_args()

    client = MongoDBConnection.get_client()
    etiquetas = nombres_tenants(client)
    dbs = sorted(d for d in client.list_database_names() if d.startswith("t_"))
    if args.tenant:
        dbs = [d for d in dbs if d == args.tenant]
        if not dbs:
            print(f"No existe la base {args.tenant}")
            return 1

    sello = datetime.now(timezone.utc)
    modo = "REVERSA" if args.revertir else "MIGRACIÓN"
    print(f"=== {modo} sin-IVA — {'APLICANDO' if args.apply else 'DRY-RUN (no escribe)'} ===\n")

    gran_total, gran_sospechosos = 0, []
    for dbname in dbs:
        db = client[dbname]
        existentes = set(db.list_collection_names())
        lineas, subtotal_tenant = [], 0
        for coll_name in COLECCIONES:
            if coll_name not in existentes:
                continue
            if args.revertir:
                ops, sospechosos, ya_ok = planear_reversa(db, coll_name), [], 0
            else:
                ops, sospechosos, ya_ok = planear(db, coll_name, sello)

            if ops and args.apply:
                res = db[coll_name].bulk_write(ops, ordered=False)
                afectados = res.modified_count
            else:
                afectados = len(ops)

            subtotal_tenant += afectados
            if ops or sospechosos:
                detalle = f"   {coll_name:<20} a modificar: {afectados}"
                if not args.revertir:
                    detalle += f"   ya alineados: {ya_ok}"
                lineas.append(detalle)
            for s in sospechosos:
                gran_sospechosos.append((dbname, coll_name) + s)

        if lineas:
            print(f"== {dbname}  {etiquetas.get(dbname, '(huérfano, sin registro en _platform)')}")
            print("\n".join(lineas))
            print()
        gran_total += subtotal_tenant

    verbo = "revertidos" if args.revertir else "migrados"
    print(f"TOTAL documentos {verbo if args.apply else 'por ' + verbo[:-1] + 'r'}: {gran_total}")

    if gran_sospechosos:
        print("\n⚠ Documentos NO tocados (total=0 con subtotal cargado) — revisar a mano:")
        for dbname, coll, folio, sub, iva, tot in gran_sospechosos:
            print(f"   {dbname} / {coll} / {folio}: subtotal={sub} iva={iva} total={tot}")

    if not args.apply:
        print("\nDry-run: no se escribió nada. Repite con --apply para aplicar.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
