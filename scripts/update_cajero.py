import os
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv()
MONGO_USER = os.environ.get("MONGO_USER")
MONGO_PASSWORD = os.environ.get("MONGO_PASSWORD")
MONGO_HOST = os.environ.get("MONGO_HOST")
MONGO_DB = os.environ.get("MONGO_DB", "siga")

uri = f"mongodb+srv://{MONGO_USER}:{MONGO_PASSWORD}@{MONGO_HOST}/{MONGO_DB}?retryWrites=true&w=majority"
client = MongoClient(uri)

# Find tenant ID from valheis@protonmail.com or omar.valdez.becerril@gmail.com
all_dbs = [d for d in client.list_database_names() if d.startswith("t_")]

found = False
for db_name in all_dbs:
    test_db = client[db_name]
    u = test_db.usuarios.find_one({"email": "valheis@protonmail.com"})
    if u:
        print(f"User valheis@protonmail.com found in {db_name}")
        test_db.usuarios.update_one(
            {"email": "valheis@protonmail.com"},
            {"$set": {"rol": "CAJERO", "grupo": "CAJERO", "activo": True}}
        )
        print("Updated role to CAJERO.")
        found = True
        break
        
if not found:
    print("User valheis@protonmail.com not found. We should find the correct tenant and create him.")
    for db_name in all_dbs:
        test_db = client[db_name]
        u = test_db.usuarios.find_one({"email": "omar.valdez.becerril@gmail.com"})
        if u:
            tenant_id = u.get("tenant_id")
            sucursal = test_db.sucursales.find_one({"tenant_id": tenant_id})
            sucursal_id = str(sucursal["_id"]) if sucursal else ""
            test_db.usuarios.insert_one({
                "email": "valheis@protonmail.com",
                "nombre": "Val",
                "apellido": "Heis",
                "rol": "CAJERO",
                "grupo": "CAJERO",
                "sucursal_id": sucursal_id,
                "activo": True,
                "tenant_id": tenant_id
            })
            print(f"Created valheis@protonmail.com as CAJERO in {db_name} with sucursal {sucursal_id}.")
            break
