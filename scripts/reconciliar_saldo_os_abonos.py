"""
reconciliar_saldo_os_abonos.py — Re-sincroniza `ordenes_servicio.saldo_pendiente`
con el saldo real de su venta.

Hasta ahora el abono a CxC (POST /ventas/{id}/pagos) sólo tocaba la OS cuando el
abono liquidaba la venta. Un abono parcial bajaba `ventas.saldo_pendiente` pero la
OS conservaba el saldo previo, así que la pestaña "Por Cobrar" de Órdenes mostraba
más deuda que Cobranza (CxC). Ya se corrigió el handler; este script arregla lo
histórico.

Fuente de verdad: la venta vigente (no CANCELADA/ANULADA) ligada por `orden_id`.
Sólo corrige OS con venta vigente y saldo positivo distinto al de la venta; las
que quedan en 0 las cierra el flujo normal del abono, no este script.

Idempotente. Uso:
  python scripts/reconciliar_saldo_os_abonos.py --env .env.prod            # dry-run, todos
  python scripts/reconciliar_saldo_os_abonos.py --env .env.prod --apply
"""
import os
import sys
import argparse
from datetime import datetime
from bson import ObjectId
from bson.errors import InvalidId
from pymongo import MongoClient
from dotenv import load_dotenv


def _client() -> MongoClient:
    user = os.environ.get("MONGO_USER")
    password = os.environ.get("MONGO_PASSWORD")
    host = os.environ.get("MONGO_HOST")
    db_name = os.environ.get("MONGO_DB", "siga")
    if not (user and password and host):
        print("[ERROR] Faltan MONGO_USER / MONGO_PASSWORD / MONGO_HOST.")
        sys.exit(1)
    return MongoClient(f"mongodb+srv://{user}:{password}@{host}/{db_name}?retryWrites=true&w=majority")


def _tenants(client):
    return [(t["tenantId"], t.get("nombreComercial", "?"))
            for t in client["_platform"]["talleres"].find({}, {"tenantId": 1, "nombreComercial": 1})
            if t.get("tenantId")]


def procesar(db, apply):
    cambios = 0
    ventas = db["ventas"].find(
        {"orden_id": {"$nin": [None, ""]}, "estado": {"$nin": ["CANCELADA", "ANULADA"]},
         "saldo_pendiente": {"$gt": 0}},
        {"folio": 1, "orden_id": 1, "saldo_pendiente": 1},
    )
    for v in ventas:
        try:
            oid = ObjectId(v["orden_id"])
        except (InvalidId, TypeError):
            continue
        os_doc = db["ordenes_servicio"].find_one({"_id": oid}, {"folio": 1, "saldo_pendiente": 1, "venta_id": 1})
        if not os_doc:
            continue
        # Si la OS apunta a otra venta (re-cobro), la venta vigente es la de la OS.
        if os_doc.get("venta_id") and str(os_doc["venta_id"]) != str(v["_id"]):
            continue
        saldo_venta = round(float(v["saldo_pendiente"]), 2)
        saldo_os = round(float(os_doc.get("saldo_pendiente") or 0), 2)
        if abs(saldo_venta - saldo_os) < 0.01:
            continue
        cambios += 1
        print(f"  {os_doc.get('folio')} / {v.get('folio')}: OS ${saldo_os:,.2f} -> venta ${saldo_venta:,.2f}")
        if apply:
            db["ordenes_servicio"].update_one(
                {"_id": oid},
                {"$set": {"saldo_pendiente": saldo_venta, "updatedAt": datetime.utcnow()}},
            )
    return cambios


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default=".env")
    ap.add_argument("--tenant")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    load_dotenv(args.env, override=True)

    client = _client()
    tenants = [(args.tenant, args.tenant)] if args.tenant else _tenants(client)
    total = 0
    for tid, nombre in tenants:
        db = client[f"t_{tid.replace('-', '')}"]
        print(f"== {nombre} ({tid})")
        total += procesar(db, args.apply)
    print(f"\n{'Aplicados' if args.apply else 'DRY-RUN, por aplicar'}: {total}")


if __name__ == "__main__":
    main()
