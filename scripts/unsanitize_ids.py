import os
import sys
from pymongo import MongoClient
from bson import ObjectId

def get_db():
    user = os.environ.get("MONGO_USER")
    password = os.environ.get("MONGO_PASSWORD")
    host = os.environ.get("MONGO_HOST")
    db_name = "t_3d1bb013a8bf453b8abbf7f0225c7b3c" # Target Tenant
    
    if not (user and password and host):
        print("❌ ERROR: Faltan variables de entorno (MONGO_USER, MONGO_PASSWORD o MONGO_HOST).")
        sys.exit(1)
        
    uri = f"mongodb+srv://{user}:{password}@{host}/?retryWrites=true&w=majority"
    client = MongoClient(uri)
    return client[db_name]

def unsanitize_collection(db, collection_name, fields):
    print(f"\n--- Revirtiendo a strings en {collection_name} ---")
    updated = 0
    
    docs = list(db[collection_name].find())
    for doc in docs:
        doc_id = doc['_id']
        updates = {}
        
        for field in fields:
            val = doc.get(field)
            if isinstance(val, ObjectId):
                updates[field] = str(val)
                
        if updates:
            db[collection_name].update_one({"_id": doc_id}, {"$set": updates})
            updated += 1
            
    print(f"  Procesados: {len(docs)}, Revertidos a string: {updated}")

def main():
    try:
        db = get_db()
        print(f"Conectado a la base de datos: {db.name}")
        
        # Campos relacionales que deben ser strings
        config = {
            "clientes": ["sucursal_id"],
            "items": ["sucursal_id"],
            "vehiculos": ["cliente_id", "sucursal_id"],
            "ordenes_servicio": ["sucursal_id", "vehiculo_id", "mecanico_id", "cita_id"],
            "citas": ["sucursal_id", "clienteId", "vehiculoId", "tecnicoId", "orden_id"],
            "ventas": ["sucursal_id", "cliente_id", "orden_id"],
            "folios": ["sucursal_id"]
        }
        
        for coll, fields in config.items():
            unsanitize_collection(db, coll, fields)
            
        print("\n✅ ¡Reversión a strings completada con éxito!")
        print("Los IDs relacionales ahora son strings de nuevo.")
        
    except Exception as e:
        print(f"❌ Error durante la reversión: {str(e)}")

if __name__ == "__main__":
    main()
