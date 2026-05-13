import sys
import os
from bson import ObjectId

# Add src to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.shared.infrastructure.database import get_tenant_db

def check():
    # Use the tenant ID from the summary or seed script
    tenant_id = "3d1bb013a8bf453b8abbf7f0225c7b3c" # From previous session summary
    db = get_tenant_db(tenant_id)
    
    print(f"Checking tenant DB: t_{tenant_id.replace('-', '')}")
    
    collections = ["clientes", "items", "sucursales", "vehiculos"]
    for coll_name in collections:
        coll = db[coll_name]
        doc = coll.find_one()
        if doc:
            print(f"\nCollection: {coll_name}")
            print(f"  _id type: {type(doc['_id'])}")
            print(f"  _id value: {doc['_id']}")
            for key in ["sucursal_id", "cliente_id", "vehiculo_id", "tenant_id"]:
                if key in doc:
                    print(f"  {key} type: {type(doc[key])}")
                    print(f"  {key} value: {doc[key]}")
        else:
            print(f"\nCollection: {coll_name} - EMPTY")

if __name__ == "__main__":
    check()
