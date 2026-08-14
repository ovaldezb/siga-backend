"""
backfill_sucursal_gastos.py — Asigna sucursal a los gastos que quedaron "generales".

Hasta ahora la UI de Contabilidad capturaba gastos fijos y variables sin mandar la
sucursal, así que el backend los guardaba con `sucursal_id` nulo y el listado los
mostraba en TODAS las sucursales (una sucursal veía la renta de la otra). Ya se
corrigió el alta y el filtro; este script reasigna lo histórico.

Cómo decide la sucursal de cada gasto, en orden:
  1. `createdBy` / `updatedBy` (email o nombre del capturista) -> su usuario en la
     colección `usuarios` -> su única sucursal asignada.
  2. `--sucursal <ID>` como destino para todo lo que quede indeterminado.
  3. Si el taller tiene UNA sola sucursal, ésa (no hay ambigüedad posible).
Lo que no se puede determinar se deja intacto y se reporta al final.

OJO — ORDEN DE DESPLIEGUE: corre esto ANTES de desplegar el backend con el filtro
estricto. Si despliegas primero, los gastos con `sucursal_id` nulo desaparecen de
la vista de ambas sucursales hasta que este backfill corra.

Idempotente: sólo toca documentos con `sucursal_id` nulo o ausente.

Uso:
  # DRY-RUN de un taller (no escribe nada):
  python scripts/backfill_sucursal_gastos.py --tenant <TENANT_ID>

  # Aplicar, mandando lo indeterminado a una sucursal concreta:
  python scripts/backfill_sucursal_gastos.py --tenant <TENANT_ID> --sucursal <SUCURSAL_ID> --apply

  # Todos los talleres:
  python scripts/backfill_sucursal_gastos.py --todos --apply

Requiere MONGO_USER / MONGO_PASSWORD / MONGO_HOST (o .env en la raíz del repo).
"""
import os
import sys
import argparse
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv()

COLECCIONES = ["gastos_fijos_mes", "gastos_variables"]

# Documentos "generales": el campo falta, es None o quedó como cadena vacía.
FILTRO_SIN_SUCURSAL = {"$or": [
    {"sucursal_id": None},
    {"sucursal_id": ""},
    {"sucursal_id": {"$exists": False}},
]}


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


def _sucursales_del_taller(db):
    """{sucursal_id: nombre} de las sucursales del tenant."""
    salida = {}
    for s in db["sucursales"].find({}, {"nombre": 1}):
        salida[str(s["_id"])] = s.get("nombre", "?")
    return salida


def _mapa_capturista_sucursal(db):
    """{email/nombre en minúsculas -> sucursal_id} para usuarios con UNA sola sucursal.

    Los usuarios con varias sucursales no desambiguan nada, así que se omiten.
    """
    mapa = {}
    for u in db["usuarios"].find({}, {"email": 1, "nombre": 1, "sucursales": 1}):
        ids = set()
        for item in u.get("sucursales") or []:
            if isinstance(item, dict):
                sid = item.get("sucursal") or item.get("id") or item.get("sucursal_id")
            else:
                sid = item
            if sid:
                ids.add(str(sid))
        if len(ids) != 1:
            continue
        sid = ids.pop()
        for clave in (u.get("email"), u.get("nombre")):
            if clave:
                mapa[str(clave).strip().lower()] = sid
    return mapa


def procesar_tenant(db, etiqueta, sucursal_default, apply):
    print(f"\n--> {etiqueta}")

    sucursales = _sucursales_del_taller(db)
    capturistas = _mapa_capturista_sucursal(db)

    # Con una sola sucursal no hay ambigüedad: todo el histórico es de ella.
    fallback = sucursal_default
    if not fallback and len(sucursales) == 1:
        fallback = next(iter(sucursales))
        print(f"     taller de una sola sucursal: se asigna todo a {sucursales[fallback]}")

    if sucursal_default and sucursales and sucursal_default not in sucursales:
        print(f"     [ERROR] --sucursal {sucursal_default} no existe en este taller. Se omite.")
        return 0, 0

    asignados = 0
    indeterminados = 0

    for coleccion in COLECCIONES:
        docs = list(db[coleccion].find(FILTRO_SIN_SUCURSAL))
        if not docs:
            continue

        por_sucursal = {}
        sin_destino = 0
        for d in docs:
            autor = (d.get("createdBy") or d.get("updatedBy") or "").strip().lower()
            destino = capturistas.get(autor) or fallback
            if not destino:
                sin_destino += 1
                continue
            por_sucursal.setdefault(destino, []).append(d["_id"])

        for sid, ids in por_sucursal.items():
            nombre = sucursales.get(sid, sid)
            print(f"     {coleccion}: {len(ids)} -> {nombre}")
            if apply:
                db[coleccion].update_many({"_id": {"$in": ids}}, {"$set": {"sucursal_id": sid}})
            asignados += len(ids)

        if sin_destino:
            print(f"     {coleccion}: {sin_destino} sin destino (capturista desconocido o con varias sucursales)")
            indeterminados += sin_destino

    if not asignados and not indeterminados:
        print("     [OK] nada pendiente.")
    return asignados, indeterminados


def main():
    parser = argparse.ArgumentParser(description="Asigna sucursal a gastos fijos y variables sin sucursal_id.")
    grupo = parser.add_mutually_exclusive_group(required=True)
    grupo.add_argument("--tenant", help="Tenant ID concreto.")
    grupo.add_argument("--todos", action="store_true", help="Todos los tenants de _platform.talleres.")
    parser.add_argument("--sucursal", help="Sucursal destino para los gastos que no se puedan determinar.")
    parser.add_argument("--apply", action="store_true", help="Aplicar cambios (sin esto es dry-run).")
    args = parser.parse_args()

    if args.todos and args.sucursal:
        print("[ERROR] --sucursal es de un taller concreto; no se combina con --todos.")
        sys.exit(1)

    modo = "APPLY (escribiendo)" if args.apply else "DRY-RUN (solo lectura)"
    client = _client()
    print(f"Conectado a MongoDB. Modo: {modo}")

    tot_a = tot_i = 0
    if args.todos:
        tenants = _listar_tenants(client)
        if not tenants:
            print("No se encontraron tenants en _platform.talleres.")
            sys.exit(1)
        for tid, nombre in tenants:
            a, i = procesar_tenant(_tenant_db(client, tid), f"{nombre} ({tid})", None, args.apply)
            tot_a += a
            tot_i += i
    else:
        tot_a, tot_i = procesar_tenant(_tenant_db(client, args.tenant), args.tenant, args.sucursal, args.apply)

    print(f"\n=== TOTAL: asignados={tot_a}  indeterminados={tot_i} ===")
    if tot_i:
        print("Los indeterminados siguen como generales. Re-corre con --tenant ... --sucursal <ID> para mandarlos a una sucursal.")
    if not args.apply and tot_a:
        print("Esto fue DRY-RUN. Re-corre con --apply para aplicar los cambios.")


if __name__ == "__main__":
    main()
