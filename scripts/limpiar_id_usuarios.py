"""
limpiar_id_usuarios.py — Quita el campo `id` guardado en los documentos de
`usuarios` de cada taller.

Al crear un taller, el ADMIN inicial se insertaba con `"id": <email>`. El id real
del usuario es su `_id`; ese campo extra se colaba en GET /usuarios y el front
mandaba el email a PUT /usuarios/{id} ("ID inválido."). El backend ya ignora el
campo; este script solo deja los datos limpios. Nada más lo lee.

Idempotente. Uso:
  python scripts/limpiar_id_usuarios.py --env .env.prod            # dry-run
  python scripts/limpiar_id_usuarios.py --env .env.prod --apply
"""
import os
import sys
import argparse
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default=".env")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    load_dotenv(args.env, override=True)

    client = _client()
    print(f"Host: {os.environ.get('MONGO_HOST')}")

    filtro = {"id": {"$exists": True}}
    total = 0
    for nombre in sorted(client.list_database_names()):
        if not nombre.startswith("t_"):
            continue
        col = client[nombre]["usuarios"]
        afectados = list(col.find(filtro, {"email": 1, "id": 1}))
        if not afectados:
            continue
        total += len(afectados)
        for u in afectados:
            print(f"  {nombre}  _id={u['_id']}  email={u.get('email')}  id={u.get('id')!r}")
        if args.apply:
            res = col.update_many(filtro, {"$unset": {"id": ""}})
            print(f"  {nombre}: {res.modified_count} actualizado(s)")

    if not total:
        print("Ningún usuario tiene el campo `id`; nada que hacer.")
    elif not args.apply:
        print(f"DRY-RUN: se quitaría el campo `id` a {total} usuario(s). Usa --apply para escribir.")
    else:
        print(f"Listo: campo `id` quitado a {total} usuario(s).")


if __name__ == "__main__":
    main()
