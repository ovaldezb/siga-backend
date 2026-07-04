"""
Diagnóstico: productos NO inventariables que ensucian el Punto de Venta.

Contexto:
  Las Órdenes de Servicio permiten capturar piezas "manuales" (que no están en el
  inventario). Con el toggle "persistir items manuales" activo, esas piezas se
  guardan en el catálogo como items PRODUCTO con maneja_inventario:false. El Punto
  de Venta las mostraba como si fueran inventario ("paja"). También pudieron entrar
  por la migración dev→prod al copiar el catálogo completo de una BD a otra.

Este script es SOLO LECTURA por defecto: recorre todos los talleres de
_platform.talleres, y por cada tenant/sucursal cuenta y muestra los PRODUCTO con
maneja_inventario:false (y, aparte, los PRODUCTO sin el campo, típicos de seeds).

Uso:
  # Requiere MONGO_USER / MONGO_PASSWORD / MONGO_HOST del cluster a inspeccionar.
  python scripts/diagnostico_items_no_inventario.py
  python scripts/diagnostico_items_no_inventario.py --tenant <TENANT_ID>
  python scripts/diagnostico_items_no_inventario.py --muestra 20   # ejemplos por tenant

  # Corrección OPCIONAL y reversible: marca activo:false los PRODUCTO
  # maneja_inventario:false (NO borra; conserva la referencia para el historial).
  # El fix de código ya los oculta del POS; esto es sólo para limpiar el listado
  # de inventario si se desea. Corre primero sin --aplicar (dry-run).
  python scripts/diagnostico_items_no_inventario.py --desactivar
  python scripts/diagnostico_items_no_inventario.py --desactivar --aplicar
"""
import os
import sys
import argparse
from pymongo import MongoClient

MONGO_USER = os.environ.get("MONGO_USER")
MONGO_PASSWORD = os.environ.get("MONGO_PASSWORD")
MONGO_HOST = os.environ.get("MONGO_HOST")
MONGO_DB = os.environ.get("MONGO_DB", "siga")


def get_client():
    if not (MONGO_USER and MONGO_PASSWORD and MONGO_HOST):
        sys.exit("Faltan MONGO_USER / MONGO_PASSWORD / MONGO_HOST en el entorno.")
    uri = f"mongodb+srv://{MONGO_USER}:{MONGO_PASSWORD}@{MONGO_HOST}/{MONGO_DB}?retryWrites=true&w=majority"
    return MongoClient(uri, serverSelectionTimeoutMS=8000)


# Producto NO inventariable explícito: la "paja" que aparece en el POS.
Q_NO_INV = {"tipo": "PRODUCTO", "maneja_inventario": False}
# Producto PRODUCTO sin el campo maneja_inventario (típico de datos sembrados/migrados).
Q_SIN_CAMPO = {"tipo": "PRODUCTO", "maneja_inventario": {"$exists": False}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", help="Limitar a un tenantId específico")
    parser.add_argument("--muestra", type=int, default=5, help="Ejemplos a mostrar por tenant")
    parser.add_argument("--desactivar", action="store_true",
                        help="Marca activo:false los PRODUCTO maneja_inventario:false")
    parser.add_argument("--aplicar", action="store_true",
                        help="Ejecuta los cambios de --desactivar (sin esto es dry-run)")
    args = parser.parse_args()

    client = get_client()
    platform = client["_platform"]

    filtro_talleres = {}
    if args.tenant:
        filtro_talleres = {"tenantId": args.tenant}

    talleres = list(platform.talleres.find(filtro_talleres))
    if not talleres:
        sys.exit("No se encontraron talleres en _platform.talleres.")

    total_no_inv = 0
    total_sin_campo = 0
    total_desactivados = 0

    for t in talleres:
        tenant_id = t.get("tenantId")
        nombre = t.get("nombreComercial") or t.get("nombre") or "(sin nombre)"
        if not tenant_id:
            continue
        db = client[f"t_{tenant_id.replace('-', '')}"]
        items = db["items"]

        n_no_inv = items.count_documents(Q_NO_INV)
        n_sin_campo = items.count_documents(Q_SIN_CAMPO)
        if n_no_inv == 0 and n_sin_campo == 0:
            continue

        total_no_inv += n_no_inv
        total_sin_campo += n_sin_campo
        print(f"\n=== {nombre}  (tenant {tenant_id}) ===")
        print(f"  PRODUCTO maneja_inventario:false ...... {n_no_inv}  <- se mostraba en POS")
        print(f"  PRODUCTO sin campo maneja_inventario .. {n_sin_campo}")

        for ej in items.find(Q_NO_INV).limit(args.muestra):
            print(f"    · {ej.get('nombre')!r}  no_parte={ej.get('no_parte')!r} "
                  f"sucursal={ej.get('sucursal_id')} stock={ej.get('stock')}")

        if args.desactivar:
            if args.aplicar:
                res = items.update_many(Q_NO_INV, {"$set": {"activo": False}})
                total_desactivados += res.modified_count
                print(f"  -> desactivados {res.modified_count} items")
            else:
                print(f"  -> [dry-run] se desactivarían {n_no_inv} items (usa --aplicar)")

    print("\n----------------------------------------")
    print(f"TOTAL PRODUCTO maneja_inventario:false : {total_no_inv}")
    print(f"TOTAL PRODUCTO sin campo               : {total_sin_campo}")
    if args.desactivar and args.aplicar:
        print(f"TOTAL desactivados                     : {total_desactivados}")


if __name__ == "__main__":
    main()
