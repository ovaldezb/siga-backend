"""
seed_modulo_kpis.py — Da de alta el módulo `kpis` en el catálogo de módulos
contratables (`_platform.modulos`).

El catálogo no vive en el código: `GET /modulos` lee esa colección y la pantalla
Talleres del SUPER_ADMIN la pinta como casillas. Mientras `kpis` no esté ahí no se
le puede activar a ningún taller, y el menú "KPIs" del front queda oculto para todos.

Idempotente. Uso:
  python scripts/seed_modulo_kpis.py --env .env.prod            # dry-run
  python scripts/seed_modulo_kpis.py --env .env.prod --apply
"""
import os
import sys
import argparse
from pymongo import MongoClient
from dotenv import load_dotenv

MODULO = "kpis"


def _client() -> MongoClient:
    user = os.environ.get("MONGO_USER")
    password = os.environ.get("MONGO_PASSWORD")
    host = os.environ.get("MONGO_HOST")
    db_name = os.environ.get("MONGO_DB", "siga")
    if not (user and password and host):
        print("[ERROR] Faltan MONGO_USER / MONGO_PASSWORD / MONGO_HOST.")
        sys.exit(1)
    return MongoClient(f"mongodb+srv://{user}:{password}@{host}/{db_name}?retryWrites=true&w=majority")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default=".env")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    load_dotenv(args.env, override=True)

    col = _client()["_platform"]["modulos"]
    actuales = sorted(m.get("modulo", "?") for m in col.find({}, {"_id": 0, "modulo": 1}))
    print(f"Host: {os.environ.get('MONGO_HOST')}")
    print(f"Catálogo actual ({len(actuales)}): {', '.join(actuales) or '(vacío)'}")

    if MODULO in actuales:
        print(f"'{MODULO}' ya existe; nada que hacer.")
        return
    if not args.apply:
        print(f"DRY-RUN: se agregaría '{MODULO}'.")
        return
    col.update_one({"modulo": MODULO}, {"$setOnInsert": {"modulo": MODULO}}, upsert=True)
    print(f"Agregado '{MODULO}'.")


if __name__ == "__main__":
    main()
