"""Backfill de índices Mongo para todos los tenants (item #17).

Cada handler de lectura llama `ensure_indexes(db, tenant_id)` en su primera
invocación tras un cold start, pero ese mecanismo es lazy: hasta que alguien
pegue al endpoint, los índices nuevos no aparecen. Este script recorre todas
las databases `t_*` en Atlas y aplica los índices al instante.

Uso (mismo set de env vars que el backend en runtime):

    MONGO_USER=... MONGO_PASSWORD=... MONGO_HOST=... \
        python scripts/ensure_indexes.py

Con `--dry-run` solo lista las databases candidatas sin tocar índices.
"""
import argparse
import os
import sys
from pymongo import MongoClient

# Permitir `from src...` ejecutando desde la raíz del repo.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.shared.utils.indexes import ensure_indexes, reset_cache  # noqa: E402


def _build_client() -> MongoClient:
    user = os.environ.get("MONGO_USER")
    password = os.environ.get("MONGO_PASSWORD")
    host = os.environ.get("MONGO_HOST")
    if not (user and password and host):
        missing = [k for k, v in {
            "MONGO_USER": user, "MONGO_PASSWORD": password, "MONGO_HOST": host
        }.items() if not v]
        print(f"FALTAN ENV VARS: {', '.join(missing)}", file=sys.stderr)
        sys.exit(2)
    uri = f"mongodb+srv://{user}:{password}@{host}/?retryWrites=true&w=majority"
    return MongoClient(uri)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Lista tenants sin crear índices")
    parser.add_argument("--tenant", help="Solo este tenant (database name completo, ej. t_abc...)")
    args = parser.parse_args()

    client = _build_client()
    # Listar databases. Atlas devuelve las del cluster + admin/local/config; filtramos.
    all_dbs = client.list_database_names()
    tenant_dbs = [d for d in all_dbs if d.startswith("t_")]
    if args.tenant:
        tenant_dbs = [d for d in tenant_dbs if d == args.tenant]

    print(f"Encontradas {len(tenant_dbs)} tenant database(s).")
    if args.dry_run:
        for d in tenant_dbs:
            print(f"  [dry-run] {d}")
        return 0

    # El cache es per-process; lo limpiamos por si el script se importa más de una vez.
    reset_cache()
    errores = 0
    for db_name in tenant_dbs:
        # ensure_indexes usa tenant_id como llave del cache; usamos el db_name (único).
        try:
            ensure_indexes(client[db_name], tenant_id=db_name)
            print(f"  ✓ {db_name}")
        except Exception as e:
            errores += 1
            print(f"  ✗ {db_name}: {e}", file=sys.stderr)

    if errores:
        print(f"\nCompletado con {errores} error(es).", file=sys.stderr)
        return 1
    print(f"\nOK — índices verificados en {len(tenant_dbs)} tenants.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
