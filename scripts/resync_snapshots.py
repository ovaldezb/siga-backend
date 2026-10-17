"""
resync_snapshots.py — Re-sincroniza las copias de cliente, vehículo y proveedor
guardadas en OS, citas, cotizaciones, ventas, compras y artículos.

Hasta el 2026-10-16 esas copias se congelaban al crear cada documento: renombrar a
un cliente (o corregir unas placas) dejaba el dato viejo en todo su historial. Los
handlers de update ya propagan los cambios (src/shared/utils/propagacion.py); este
script aplica la misma regla a lo que quedó desfasado antes del arreglo.

Usa los mismos campos que la propagación en vivo (identidad y contacto). No toca
kilometrajes, montos, CFDI ni el nombre del mecánico (ver propagacion.py). Compara en memoria (catálogos cargados una vez por
taller) y escribe con bulk_write sólo los documentos que difieren.

Idempotente.

Uso:
  python scripts/resync_snapshots.py --todos                  # DRY-RUN dev
  python scripts/resync_snapshots.py --todos --env .env.prod  # DRY-RUN prod
  python scripts/resync_snapshots.py --todos --env .env.prod --apply
"""
import os
import sys
import argparse
from collections import Counter
from pymongo import MongoClient, UpdateOne
from dotenv import dotenv_values

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from src.shared.utils.propagacion import (  # noqa: E402
    CAMPOS_CLIENTE, CAMPOS_VEHICULO, CAMPOS_PROVEEDOR,
    nombre_completo_cliente, descripcion_vehiculo,
)


def _client(env_file):
    env = dotenv_values(env_file)
    user, password, host = env.get("MONGO_USER"), env.get("MONGO_PASSWORD"), env.get("MONGO_HOST")
    if not (user and password and host):
        print(f"[ERROR] Faltan MONGO_USER / MONGO_PASSWORD / MONGO_HOST en {env_file}.")
        sys.exit(1)
    db_name = env.get("MONGO_DB", "siga")
    return MongoClient(f"mongodb+srv://{user}:{password}@{host}/{db_name}?retryWrites=true&w=majority"), host


def _diff_snapshot(actual, fuente, campos, prefijo):
    """$set con los campos del catálogo que difieren de la copia (sólo los que el
    catálogo tiene; un campo ausente en el catálogo no borra nada)."""
    if not isinstance(actual, dict):
        return {}
    return {f"{prefijo}.{c}": fuente.get(c) for c in campos
            if c in fuente and actual.get(c) != fuente.get(c)
            # Un vacío del catálogo no agrega una llave que la copia nunca tuvo.
            and (c in actual or fuente.get(c) not in (None, ""))}


def _diff_plano(doc, valores):
    return {k: v for k, v in valores.items() if v and doc.get(k) != v}


def _aplicar(db, coleccion, ops, aplicar, stats, etiqueta):
    if not ops:
        return
    stats[etiqueta] += len(ops)
    if aplicar:
        for i in range(0, len(ops), 500):
            db[coleccion].bulk_write(ops[i:i + 500], ordered=False)


def resync_tenant(db, aplicar, stats):
    clientes = {str(c["_id"]): c for c in db.clientes.find({}, {c: 1 for c in CAMPOS_CLIENTE})}
    vehiculos = {str(v["_id"]): v for v in db.vehiculos.find({}, {c: 1 for c in CAMPOS_VEHICULO})}
    proveedores = {str(p["_id"]): p for p in db.proveedores.find({}, {c: 1 for c in CAMPOS_PROVEEDOR})}

    # OS y cotizaciones: cliente_snapshot + vehiculo_snapshot.
    for coleccion in ("ordenes_servicio", "cotizaciones"):
        ops = []
        for d in db[coleccion].find({}, {"cliente_snapshot": 1, "cliente_id": 1, "vehiculo_id": 1,
                                         "vehiculo_snapshot": 1}):
            cs = d.get("cliente_snapshot") if isinstance(d.get("cliente_snapshot"), dict) else {}
            vs = d.get("vehiculo_snapshot") if isinstance(d.get("vehiculo_snapshot"), dict) else {}
            cli = clientes.get(cs.get("id") or d.get("cliente_id") or "")
            veh = vehiculos.get(d.get("vehiculo_id") or vs.get("id") or "")
            cambios = {}
            if cli and cs:
                cambios.update(_diff_snapshot(cs, cli, CAMPOS_CLIENTE, "cliente_snapshot"))
            if veh and vs:
                cambios.update(_diff_snapshot(vs, veh, CAMPOS_VEHICULO, "vehiculo_snapshot"))
            if cambios:
                ops.append(UpdateOne({"_id": d["_id"]}, {"$set": cambios}))
        _aplicar(db, coleccion, ops, aplicar, stats, coleccion)

    # Ventas: nombre completo del cliente + vehiculo_snapshot.
    ops = []
    for d in db.ventas.find({}, {"cliente_id": 1, "cliente_nombre": 1, "vehiculo_id": 1, "vehiculo_snapshot": 1}):
        cambios = {}
        cli = clientes.get(d.get("cliente_id") or "")
        if cli:
            cambios.update(_diff_plano(d, {"cliente_nombre": nombre_completo_cliente(cli)}))
        veh = vehiculos.get(d.get("vehiculo_id") or "")
        if veh:
            cambios.update(_diff_snapshot(d.get("vehiculo_snapshot"), veh, CAMPOS_VEHICULO, "vehiculo_snapshot"))
        if cambios:
            ops.append(UpdateOne({"_id": d["_id"]}, {"$set": cambios}))
    _aplicar(db, "ventas", ops, aplicar, stats, "ventas")

    # Citas: textos planos.
    ops = []
    for d in db.citas.find({}, {"clienteId": 1, "clienteNombre": 1, "vehiculoId": 1, "vehiculoDesc": 1,
                                "vehiculoAnio": 1}):
        valores = {}
        cli = clientes.get(d.get("clienteId") or "")
        if cli:
            valores["clienteNombre"] = nombre_completo_cliente(cli)
        veh = vehiculos.get(d.get("vehiculoId") or "")
        if veh:
            valores["vehiculoDesc"] = descripcion_vehiculo(veh)
            valores["vehiculoAnio"] = veh.get("anio")
        cambios = _diff_plano(d, valores)
        if cambios:
            ops.append(UpdateOne({"_id": d["_id"]}, {"$set": cambios}))
    _aplicar(db, "citas", ops, aplicar, stats, "citas")

    # Compras (proveedor_snapshot) y artículos (nombre del proveedor).
    ops = []
    for d in db.compras.find({"proveedor_id": {"$in": list(proveedores)}}, {"proveedor_id": 1, "proveedor_snapshot": 1}):
        cambios = _diff_snapshot(d.get("proveedor_snapshot"), proveedores[d["proveedor_id"]],
                                 CAMPOS_PROVEEDOR, "proveedor_snapshot")
        if cambios:
            ops.append(UpdateOne({"_id": d["_id"]}, {"$set": cambios}))
    _aplicar(db, "compras", ops, aplicar, stats, "compras")

    ops = []
    for d in db["items"].find({"proveedor_id": {"$in": list(proveedores)}}, {"proveedor_id": 1, "proveedor": 1}):
        cambios = _diff_plano(d, {"proveedor": (proveedores[d["proveedor_id"]].get("nombre") or "").strip()})
        if cambios:
            ops.append(UpdateOne({"_id": d["_id"]}, {"$set": cambios}))
    _aplicar(db, "items", ops, aplicar, stats, "items")


def main():
    parser = argparse.ArgumentParser(description="Re-sincroniza snapshots de cliente/vehículo/proveedor.")
    grupo = parser.add_mutually_exclusive_group(required=True)
    grupo.add_argument("--tenant", help="Tenant ID concreto.")
    grupo.add_argument("--todos", action="store_true", help="Todos los tenants de _platform.talleres.")
    parser.add_argument("--env", default=".env", help="Credenciales (.env = dev, .env.prod = prod).")
    parser.add_argument("--apply", action="store_true", help="Aplicar cambios (sin esto es dry-run).")
    args = parser.parse_args()

    client, host = _client(args.env)
    print(f"Mongo: {host}  ({args.env})  Modo: {'APPLY (escribiendo)' if args.apply else 'DRY-RUN (solo lectura)'}",
          flush=True)
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
        resync_tenant(db, args.apply, stats)
        if stats:
            print(f"\n--> {nombre} ({tid})", flush=True)
            for k in sorted(stats):
                print(f"     {k}: {stats[k]} documento(s)", flush=True)
        total.update(stats)
    print(f"\n=== TOTAL {dict(total)} ===")
    if not args.apply:
        print("Esto fue DRY-RUN (documentos que cambiarían). Re-corre con --apply para aplicar.")


if __name__ == "__main__":
    main()
