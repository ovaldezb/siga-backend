"""Limpieza del catálogo: productos con stock 0 que nunca se usaron.

Contexto:
  Las pruebas iniciales y las inyecciones de catálogo entre sucursales dejaron
  cientos de artículos en 0 que nadie compró, vendió ni movió nunca. Ensucian el
  buscador del Punto de Venta y del alta de compras. Con el alta multi-sucursal
  (un número de parte se da de alta en todas las sucursales) ya no hace falta
  arrastrar esa "paja" histórica.

Qué borra (y qué NO):
  · SÓLO artículos tipo PRODUCTO con stock 0 o nulo.
  · SÓLO si su id NO aparece referenciado en ninguna venta, orden de servicio,
    cotización, compra, traspaso ni movimiento de inventario. Un artículo con
    historial se conserva SIEMPRE, aunque hoy esté en 0: borrarlo rompería los
    reportes de contabilidad y el kardex.
  · Los SERVICIOS (mano de obra) nunca se tocan.

Es SOLO LECTURA por defecto (dry-run): sin `--aplicar` únicamente reporta.

Uso:
  # Requiere MONGO_USER / MONGO_PASSWORD / MONGO_HOST del cluster a limpiar.
  python scripts/limpiar_items_stock_cero.py                       # dry-run, todos los talleres
  python scripts/limpiar_items_stock_cero.py --tenant <TENANT_ID>  # dry-run de un taller
  python scripts/limpiar_items_stock_cero.py --sucursal <ID>       # acota a una sucursal
  python scripts/limpiar_items_stock_cero.py --antes-de 2026-01-01 # sólo los creados antes
  python scripts/limpiar_items_stock_cero.py --tenant <ID> --aplicar  # ejecuta el borrado
"""
import os
import re
import sys
import argparse
from pymongo import MongoClient

MONGO_USER = os.environ.get("MONGO_USER")
MONGO_PASSWORD = os.environ.get("MONGO_PASSWORD")
MONGO_HOST = os.environ.get("MONGO_HOST")
MONGO_DB = os.environ.get("MONGO_DB", "siga")

# Colecciones que pueden referenciar un artículo del catálogo. Se recorren enteras
# recolectando cualquier ObjectId en texto: así se cubren tanto `items[].item_id`
# como las estructuras anidadas de las órdenes (piezas, refacciones, revisión…).
COLECCIONES_CON_REFERENCIAS = (
    "ventas", "ordenes", "cotizaciones", "compras",
    "traspasos", "inventario_movimientos",
)

RE_OBJECT_ID = re.compile(r"\b[0-9a-fA-F]{24}\b")


def get_client():
    if not (MONGO_USER and MONGO_PASSWORD and MONGO_HOST):
        sys.exit("Faltan MONGO_USER / MONGO_PASSWORD / MONGO_HOST en el entorno.")
    uri = (f"mongodb+srv://{MONGO_USER}:{MONGO_PASSWORD}@{MONGO_HOST}/"
           f"{MONGO_DB}?retryWrites=true&w=majority")
    return MongoClient(uri, serverSelectionTimeoutMS=8000)


def ids_referenciados(db):
    """Todo id de 24 hex que aparezca en las colecciones operativas del taller."""
    referenciados = set()
    for nombre in COLECCIONES_CON_REFERENCIAS:
        try:
            for doc in db[nombre].find({}):
                referenciados.update(RE_OBJECT_ID.findall(str(doc)))
        except Exception as err:  # colección inexistente en tenants viejos
            print(f"    (aviso) no se pudo leer '{nombre}': {err}")
    return referenciados


def nombres_sucursales(db):
    try:
        return {str(s["_id"]): s.get("nombre") or "(sin nombre)"
                for s in db["sucursales"].find({}, {"nombre": 1})}
    except Exception:
        return {}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", help="Limitar a un tenantId específico")
    parser.add_argument("--sucursal", help="Limitar a una sucursal_id específica")
    parser.add_argument("--antes-de", dest="antes_de",
                        help="Sólo artículos con createdAt anterior a esta fecha (YYYY-MM-DD)")
    parser.add_argument("--muestra", type=int, default=10,
                        help="Ejemplos a listar por taller (default 10)")
    parser.add_argument("--aplicar", action="store_true",
                        help="Ejecuta el borrado (sin esto es dry-run)")
    args = parser.parse_args()

    client = get_client()
    platform = client["_platform"]

    talleres = list(platform.talleres.find({"tenantId": args.tenant} if args.tenant else {}))
    if not talleres:
        sys.exit("No se encontraron talleres en _platform.talleres.")

    total_candidatos = 0
    total_protegidos = 0
    total_borrados = 0

    for t in talleres:
        tenant_id = t.get("tenantId")
        nombre_taller = t.get("nombreComercial") or t.get("nombre") or "(sin nombre)"
        if not tenant_id:
            continue

        db = client[f"t_{tenant_id.replace('-', '')}"]
        items = db["items"]

        filtro = {
            "tipo": "PRODUCTO",
            "$or": [{"stock": 0}, {"stock": None}, {"stock": {"$exists": False}}],
        }
        if args.sucursal:
            filtro["sucursal_id"] = args.sucursal
        if args.antes_de:
            filtro["createdAt"] = {"$lt": args.antes_de}

        en_cero = list(items.find(filtro))
        if not en_cero:
            continue

        print(f"\n=== {nombre_taller}  (tenant {tenant_id}) ===")
        print(f"  Productos en stock 0 .................. {len(en_cero)}")

        usados = ids_referenciados(db)
        sucursales = nombres_sucursales(db)

        borrables = [i for i in en_cero if str(i["_id"]) not in usados]
        protegidos = len(en_cero) - len(borrables)
        total_candidatos += len(borrables)
        total_protegidos += protegidos

        print(f"  Con historial (se conservan) .......... {protegidos}")
        print(f"  Sin uso, borrables ................... {len(borrables)}")

        for ej in borrables[:args.muestra]:
            suc = sucursales.get(str(ej.get("sucursal_id")), ej.get("sucursal_id"))
            print(f"    · {ej.get('nombre')!r}  no_parte={ej.get('no_parte')!r}  sucursal={suc}")
        if len(borrables) > args.muestra:
            print(f"    … y {len(borrables) - args.muestra} más")

        if not borrables:
            continue

        if args.aplicar:
            res = items.delete_many({"_id": {"$in": [i["_id"] for i in borrables]}})
            total_borrados += res.deleted_count
            print(f"  -> BORRADOS {res.deleted_count} artículos")
        else:
            print(f"  -> [dry-run] se borrarían {len(borrables)} artículos (usa --aplicar)")

    print("\n----------------------------------------")
    print(f"TOTAL borrables (sin historial) : {total_candidatos}")
    print(f"TOTAL conservados (con uso)     : {total_protegidos}")
    if args.aplicar:
        print(f"TOTAL borrados                  : {total_borrados}")
    else:
        print("Dry-run: no se modificó nada. Añade --aplicar para ejecutar.")


if __name__ == "__main__":
    main()
