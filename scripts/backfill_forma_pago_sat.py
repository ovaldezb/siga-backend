"""
backfill_forma_pago_sat.py — Código SAT (c_FormaPago) en métodos de pago y ventas.

Los talleres dados de alta antes del campo `codigo_sat` tienen sus métodos de
pago sin él, así que el POS guardaba las ventas con `forma_pago_sat` vacío y la
factura tenía que adivinarlo (y adivinaba mal el crédito: salía como tarjeta).

Qué hace, por taller:
  1. configuracion.metodos_pago[]: rellena `codigo_sat` donde falta (efectivo 01,
     tarjeta 04, transferencia 03, crédito 99, o deducido del nombre: débito 28,
     cheque 02...) y separa ids repetidos (dos métodos 'tarjeta' se pisaban en el POS).
  2. ventas: rellena `pagos[].forma_pago_sat` y `forma_pago_sat` vacíos o ausentes
     con el código del método con que se cobró.

No toca:
  - un código ya capturado (sólo rellena vacíos),
  - importes, métodos, folios ni nada más de la venta,
  - ventas ya facturadas (`venta_facturada: true`): su forma de pago es la del CFDI
    timbrado y no se reescribe; sólo se reportan.
Lo que no se puede deducir (método desconocido) se deja vacío y se reporta.

Idempotente. Cada venta se actualiza condicionada a que sus `pagos` sigan igual
que cuando se leyeron, para no pisar un cobro que ocurra mientras corre.

Uso:
  python scripts/backfill_forma_pago_sat.py --todos                 # DRY-RUN dev (.env)
  python scripts/backfill_forma_pago_sat.py --todos --apply
  python scripts/backfill_forma_pago_sat.py --todos --env .env.prod # DRY-RUN prod
  python scripts/backfill_forma_pago_sat.py --tenant <TENANT_ID> --env .env.prod --apply
"""
import os
import sys
import argparse
from collections import Counter
from pymongo import MongoClient
from dotenv import dotenv_values

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from src.shared.utils.formas_pago_sat import (  # noqa: E402
    resolver_forma_pago_sat, completar_codigos_config, deduplicar_ids,
)

FILTRO_SIN_SAT = {"$or": [
    {"forma_pago_sat": {"$exists": False}},
    {"forma_pago_sat": None},
    {"forma_pago_sat": ""},
    {"pagos": {"$elemMatch": {"$or": [
        {"forma_pago_sat": {"$exists": False}},
        {"forma_pago_sat": None},
        {"forma_pago_sat": ""},
    ]}}},
]}


def _client(env_file):
    env = dotenv_values(env_file)
    user, password, host = env.get("MONGO_USER"), env.get("MONGO_PASSWORD"), env.get("MONGO_HOST")
    if not (user and password and host):
        print(f"[ERROR] Faltan MONGO_USER / MONGO_PASSWORD / MONGO_HOST en {env_file}.")
        sys.exit(1)
    db_name = env.get("MONGO_DB", "siga")
    return MongoClient(f"mongodb+srv://{user}:{password}@{host}/{db_name}?retryWrites=true&w=majority"), host


def _tenant_db(client, tenant_id):
    return client[f"t_{tenant_id.replace('-', '')}"]


def _listar_tenants(client):
    talleres = client["_platform"]["talleres"].find({}, {"tenantId": 1, "nombreComercial": 1})
    return [(t["tenantId"], t.get("nombreComercial", "?")) for t in talleres if t.get("tenantId")]


def procesar_config(db, tenant_id, apply):
    cfg = db["configuracion"].find_one({"tenant_id": tenant_id}, {"metodos_pago": 1})
    if not cfg:
        print("     config: no existe (se crea con códigos al abrir Configuración).")
        return []
    originales = cfg.get("metodos_pago") or []
    metodos, dedup = deduplicar_ids(originales)
    metodos, completados = completar_codigos_config(metodos)
    for antes, despues in zip(originales, metodos):
        if antes != despues:
            print(f"     config: {antes.get('nombre')!r}: id {antes.get('id')!r}->{despues.get('id')!r}, "
                  f"sat {antes.get('codigo_sat', '<falta>')!r}->{despues.get('codigo_sat')!r}")
    for m in metodos:
        if not m.get("codigo_sat"):
            print(f"     config: [WARN] {m.get('nombre')!r} sin código deducible; capturarlo en Configuración.")
    if (dedup or completados) and apply:
        db["configuracion"].update_one(
            {"_id": cfg["_id"], "metodos_pago": originales},
            {"$set": {"metodos_pago": metodos}},
        )
    # Las ventas se resuelven contra la config ya completada (aunque sea dry-run).
    return metodos


def procesar_ventas(db, metodos_cfg, apply):
    actualizadas = facturadas = 0
    sin_deducir = Counter()
    asignados = Counter()
    for v in db["ventas"].find(FILTRO_SIN_SAT, {"pagos": 1, "forma_pago_sat": 1, "metodo_pago": 1,
                                                "venta_facturada": 1, "folio": 1}):
        if v.get("venta_facturada"):
            facturadas += 1
            continue
        pagos_orig = v.get("pagos") or []
        pagos = []
        for p in pagos_orig:
            if isinstance(p, dict) and not p.get("forma_pago_sat"):
                codigo = resolver_forma_pago_sat(p.get("metodo"), metodos_cfg)
                if codigo:
                    p = {**p, "forma_pago_sat": codigo}
                    asignados[(str(p.get("metodo")), codigo)] += 1
                else:
                    sin_deducir[str(p.get("metodo"))] += 1
            pagos.append(p)
        set_doc = {}
        if pagos != pagos_orig:
            set_doc["pagos"] = pagos
        if not v.get("forma_pago_sat"):
            codigo = next((p.get("forma_pago_sat") for p in pagos if isinstance(p, dict) and p.get("forma_pago_sat")), "")
            codigo = codigo or resolver_forma_pago_sat(v.get("metodo_pago"), metodos_cfg)
            if codigo:
                set_doc["forma_pago_sat"] = codigo
            else:
                sin_deducir[f"venta:{v.get('metodo_pago')}"] += 1
        if not set_doc:
            continue
        actualizadas += 1
        if apply:
            filtro = {"_id": v["_id"], "venta_facturada": {"$ne": True}}
            filtro["pagos"] = pagos_orig if "pagos" in v else {"$exists": False}
            if "forma_pago_sat" in v:
                filtro["forma_pago_sat"] = v["forma_pago_sat"]
            else:
                filtro["forma_pago_sat"] = {"$exists": False}
            res = db["ventas"].update_one(filtro, {"$set": set_doc})
            if res.matched_count == 0:
                print(f"     [SKIP] {v.get('folio')}: cambió mientras corría el script.")
                actualizadas -= 1
    for (metodo, codigo), n in sorted(asignados.items()):
        print(f"     pagos: metodo={metodo!r} -> {codigo}  x{n}")
    print(f"     ventas a actualizar: {actualizadas}   facturadas omitidas: {facturadas}")
    for metodo, n in sin_deducir.items():
        print(f"     [WARN] sin código deducible: {metodo!r} x{n}")
    return actualizadas, facturadas, sum(sin_deducir.values())


def main():
    parser = argparse.ArgumentParser(description="Backfill del código SAT de forma de pago.")
    grupo = parser.add_mutually_exclusive_group(required=True)
    grupo.add_argument("--tenant", help="Tenant ID concreto.")
    grupo.add_argument("--todos", action="store_true", help="Todos los tenants de _platform.talleres.")
    parser.add_argument("--env", default=".env", help="Archivo de credenciales (.env = dev, .env.prod = prod).")
    parser.add_argument("--apply", action="store_true", help="Aplicar cambios (sin esto es dry-run).")
    args = parser.parse_args()

    client, host = _client(args.env)
    print(f"Mongo: {host}  ({args.env})  Modo: {'APPLY (escribiendo)' if args.apply else 'DRY-RUN (solo lectura)'}")

    tenants = _listar_tenants(client) if args.todos else [(args.tenant, args.tenant)]
    tot = Counter()
    for tid, nombre in tenants:
        print(f"\n--> {nombre} ({tid})")
        db = _tenant_db(client, tid)
        metodos = procesar_config(db, tid, args.apply)
        a, f, s = procesar_ventas(db, metodos, args.apply)
        tot["ventas"] += a
        tot["facturadas"] += f
        tot["sin_deducir"] += s

    print(f"\n=== TOTAL ventas={tot['ventas']} facturadas_omitidas={tot['facturadas']} "
          f"sin_deducir={tot['sin_deducir']} ===")
    if not args.apply:
        print("Esto fue DRY-RUN. Re-corre con --apply para aplicar los cambios.")


if __name__ == "__main__":
    main()
