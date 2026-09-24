"""
backfill_conciliacion_caja.py — Código SAT en movimientos de caja y abonos históricos.

El corte de caja y sus reportes ahora se concilian por forma de pago SAT (efectivo
01, tarjeta 04/28, transferencia 03…). Los movimientos viejos sólo tienen `metodo`,
y en varios talleres ese método es el id de un método propio que ya no existe en la
configuración ("1787351150203"), así que no se puede deducir de ahí. Este script
toma el código del pago de la venta a la que pertenece cada movimiento.

Qué hace, por taller:
  1. caja_sesiones.movimientos[] (abiertas y cerradas) de tipo VENTA sin
     `forma_pago_sat`: el código del pago correspondiente de su venta; si no hay
     venta, el deducido del método. Las ENTRADAS "Abono CxC" viejas eran siempre
     efectivo (01).
  2. ventas.abonos[] sin `forma_pago_sat`: el deducido de su método.

No toca: montos, total_ventas/entradas/salidas, arqueos, diferencias ni nada firmado
del corte; sólo agrega el campo `forma_pago_sat` donde falta. Un pago con código 99
de una venta que no generó crédito (métodos propios anteriores al arreglo) se deja
sin código: entró a caja como cobrado y se reporta para revisión.

Idempotente; cada documento se actualiza condicionado a que no haya cambiado.

Uso:
  python scripts/backfill_conciliacion_caja.py --todos                  # DRY-RUN dev
  python scripts/backfill_conciliacion_caja.py --todos --env .env.prod  # DRY-RUN prod
  python scripts/backfill_conciliacion_caja.py --todos --env .env.prod --apply
"""
import os
import sys
import argparse
from collections import Counter
from bson import ObjectId
from bson.errors import InvalidId
from pymongo import MongoClient
from dotenv import dotenv_values

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from src.shared.utils.formas_pago_sat import (  # noqa: E402
    resolver_forma_pago_sat, es_credito, forma_pago_de, CODIGO_CREDITO,
)


def _client(env_file):
    env = dotenv_values(env_file)
    user, password, host = env.get("MONGO_USER"), env.get("MONGO_PASSWORD"), env.get("MONGO_HOST")
    if not (user and password and host):
        print(f"[ERROR] Faltan MONGO_USER / MONGO_PASSWORD / MONGO_HOST en {env_file}.")
        sys.exit(1)
    db_name = env.get("MONGO_DB", "siga")
    return MongoClient(f"mongodb+srv://{user}:{password}@{host}/{db_name}?retryWrites=true&w=majority"), host


def _metodos_cfg(db, tenant_id):
    cfg = db["configuracion"].find_one({"tenant_id": tenant_id}, {"metodos_pago": 1}) or {}
    return cfg.get("metodos_pago") or []


def _contado_de_venta(venta):
    """Pagos que entraron a caja, en el orden en que se generaron sus movimientos."""
    sin_cxc = float(venta.get("monto_credito") or 0) <= 0
    return [p for p in (venta.get("pagos") or []) if isinstance(p, dict)
            and float(p.get("monto") or 0) > 0 and (sin_cxc or not es_credito(p))]


def procesar_caja(db, metodos_cfg, apply, stats):
    ventas_cache = {}

    def venta(vid):
        if vid not in ventas_cache:
            try:
                ventas_cache[vid] = db["ventas"].find_one(
                    {"_id": ObjectId(vid)}, {"pagos": 1, "monto_credito": 1, "forma_pago_sat": 1, "folio": 1})
            except (InvalidId, TypeError):
                ventas_cache[vid] = None
        return ventas_cache[vid]

    for sesion in db.caja_sesiones.find({"movimientos": {"$elemMatch": {
            "forma_pago_sat": {"$exists": False}, "tipo": {"$in": ["VENTA", "ENTRADA"]}}}},
            {"movimientos": 1, "estado": 1}):
        originales = sesion.get("movimientos") or []
        movs = [dict(m) for m in originales]
        usados = Counter()  # venta_id -> cuántos movimientos de esa venta ya se emparejaron
        cambio = False
        for mov in movs:
            if mov.get("forma_pago_sat") is not None:
                if mov.get("tipo") == "VENTA" and mov.get("venta_id"):
                    usados[mov["venta_id"]] += 1
                continue
            tipo = mov.get("tipo")
            codigo = None
            if tipo == "VENTA":
                v = venta(mov.get("venta_id")) if mov.get("venta_id") else None
                if v:
                    contado = _contado_de_venta(v)
                    idx = usados[mov["venta_id"]]
                    usados[mov["venta_id"]] += 1
                    pago = contado[idx] if idx < len(contado) else None
                    if pago and str(pago.get("metodo") or "").upper() == str(mov.get("metodo") or "").upper():
                        codigo = forma_pago_de(pago, metodos_cfg)
                    elif not contado and v.get("forma_pago_sat"):
                        codigo = v["forma_pago_sat"]
                if codigo is None:
                    codigo = resolver_forma_pago_sat(mov.get("metodo"), metodos_cfg)
                if codigo == CODIGO_CREDITO:
                    stats["caja_99_revisar"] += 1
                    stats[f"revisar:{(v or {}).get('folio') or mov.get('venta_folio')}"] += 1
                    continue
            elif tipo == "ENTRADA" and str(mov.get("concepto") or "").startswith("Abono CxC"):
                codigo = "01"  # antes del arreglo sólo los abonos en efectivo entraban a caja
                mov.setdefault("metodo", "EFECTIVO")
            else:
                continue  # entradas manuales: efectivo del cajón, no llevan método
            if not codigo:
                stats["caja_sin_codigo"] += 1
                continue
            mov["forma_pago_sat"] = codigo
            stats[f"caja:{codigo}"] += 1
            cambio = True
        if cambio:
            stats["sesiones"] += 1
            if apply:
                res = db.caja_sesiones.update_one(
                    {"_id": sesion["_id"], "movimientos": originales},
                    {"$set": {"movimientos": movs}})
                if res.matched_count == 0:
                    print(f"     [SKIP] sesión {sesion['_id']}: cambió mientras corría el script.")


def procesar_abonos(db, metodos_cfg, apply, stats):
    for v in db["ventas"].find({"abonos": {"$elemMatch": {"forma_pago_sat": {"$exists": False}}}},
                               {"abonos": 1}):
        originales = v["abonos"]
        abonos = []
        for a in originales:
            if isinstance(a, dict) and "forma_pago_sat" not in a:
                codigo = resolver_forma_pago_sat(a.get("metodo"), metodos_cfg)
                if codigo and codigo != CODIGO_CREDITO:
                    a = {**a, "forma_pago_sat": codigo}
                    stats[f"abono:{codigo}"] += 1
                else:
                    stats["abono_sin_codigo"] += 1
            abonos.append(a)
        if abonos != originales:
            stats["ventas_con_abonos"] += 1
            if apply:
                db["ventas"].update_one({"_id": v["_id"], "abonos": originales}, {"$set": {"abonos": abonos}})


def main():
    parser = argparse.ArgumentParser(description="Código SAT en movimientos de caja y abonos históricos.")
    grupo = parser.add_mutually_exclusive_group(required=True)
    grupo.add_argument("--tenant", help="Tenant ID concreto.")
    grupo.add_argument("--todos", action="store_true", help="Todos los tenants de _platform.talleres.")
    parser.add_argument("--env", default=".env", help="Credenciales (.env = dev, .env.prod = prod).")
    parser.add_argument("--apply", action="store_true", help="Aplicar cambios (sin esto es dry-run).")
    args = parser.parse_args()

    client, host = _client(args.env)
    print(f"Mongo: {host}  ({args.env})  Modo: {'APPLY (escribiendo)' if args.apply else 'DRY-RUN (solo lectura)'}")
    if args.todos:
        tenants = [(t["tenantId"], t.get("nombreComercial", "?"))
                   for t in client["_platform"]["talleres"].find({}, {"tenantId": 1, "nombreComercial": 1})
                   if t.get("tenantId")]
    else:
        tenants = [(args.tenant, args.tenant)]

    total = Counter()
    for tid, nombre in tenants:
        db = client[f"t_{tid.replace('-', '')}"]
        stats = Counter()
        cfg = _metodos_cfg(db, tid)
        procesar_caja(db, cfg, args.apply, stats)
        procesar_abonos(db, cfg, args.apply, stats)
        if stats:
            print(f"\n--> {nombre} ({tid})")
            for k in sorted(stats):
                if not k.startswith("revisar:"):
                    print(f"     {k}: {stats[k]}")
            revisar = sorted(k.split(":", 1)[1] for k in stats if k.startswith("revisar:"))
            if revisar:
                print(f"     [REVISAR] cobros con código 99 que entraron a caja como pagados: {', '.join(revisar)}")
        total.update({k: v for k, v in stats.items() if not k.startswith("revisar:")})
    print(f"\n=== TOTAL {dict(total)} ===")
    if not args.apply:
        print("Esto fue DRY-RUN. Re-corre con --apply para aplicar los cambios.")


if __name__ == "__main__":
    main()
