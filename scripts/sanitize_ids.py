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
        print("Faltan variables de entorno para la conexión.")
        sys.exit(1)
        
    uri = f"mongodb+srv://{user}:{password}@{host}/?retryWrites=true&w=majority"
    client = MongoClient(uri)
    return client[db_name]

def is_valid_oid(val):
    if not isinstance(val, str) or len(val) != 24:
        return False
    try:
        ObjectId(val)
        return True
    except:
        return False

def sanitize_collection(db, collection_name, fields):
    print(f"\n--- Sanitizando {collection_name} ---")
    count = 0
    updated = 0
    
    docs = list(db[collection_name].find())
    for doc in docs:
        doc_id = doc['_id']
        updates = {}
        
        # 1. Verificar _id (por si acaso fuera string)
        if isinstance(doc_id, str) and is_valid_oid(doc_id):
             # Esto es complejo porque requiere reinsertar el doc
             print(f"  [WARN] Documento con _id string detectado: {doc_id}. Omitiendo por seguridad.")
             
        # 2. Sanitizar campos relacionales
        for field in fields:
            val = doc.get(field)
            if val and is_valid_oid(val):
                updates[field] = ObjectId(val)
                
        if updates:
            db[collection_name].update_one({"_id": doc_id}, {"$set": updates})
            updated += 1
            
    print(f"  Procesados: {len(docs)}, Actualizados: {updated}")

def main():
    db = get_db()
    print(f"Conectado a la base de datos: {db.name}")
    
    # Definición de campos relacionales por colección
    config = {
        "clientes": ["sucursal_id"],
        "items": ["sucursal_id"],
        "vehiculos": ["cliente_id", "sucursal_id"],
        "ordenes_servicio": ["sucursal_id", "vehiculo_id", "mecanico_id", "cita_id"],
        "citas": ["sucursal_id", "clienteId", "vehiculoId", "tecnicoId", "orden_id"],
        "ventas": ["sucursal_id", "cliente_id", "orden_id"]
    }
    
    for coll, fields in config.items():
        sanitize_collection(db, coll, fields)
        
    print("\n¡Sanitización completada!")

if __name__ == "__main__":
    main()
