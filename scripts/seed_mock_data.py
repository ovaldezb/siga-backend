import os
import random
import uuid
from datetime import datetime, timedelta
from pymongo import MongoClient, ReturnDocument
from bson import ObjectId

# Configuración (Ajustar si es necesario)
MONGO_USER = os.environ.get("MONGO_USER")
MONGO_PASSWORD = os.environ.get("MONGO_PASSWORD")
MONGO_HOST = os.environ.get("MONGO_HOST")
MONGO_DB = os.environ.get("MONGO_DB", "siga")

if not all([MONGO_USER, MONGO_PASSWORD, MONGO_HOST]):
    print("Error: Faltan variables de entorno (MONGO_USER, MONGO_PASSWORD, MONGO_HOST)")
    exit(1)

uri = f"mongodb+srv://{MONGO_USER}:{MONGO_PASSWORD}@{MONGO_HOST}/{MONGO_DB}?retryWrites=true&w=majority"
client = MongoClient(uri)

# Datos semilla
TENANT_ID = "6fb63f90-8e1e-4b6a-9f4a-8f8f8f8f8f8f" # Cambiar por uno real si existe
SAFE_TENANT_ID = TENANT_ID.replace("-", "")
db = client[f"t_{SAFE_TENANT_ID}"]

MARCAS = ["Toyota", "Honda", "Nissan", "Ford", "Chevrolet", "VW", "Mazda", "BMW", "Audi"]
MODELOS = {
    "Toyota": ["Corolla", "Hilux", "Tacoma", "Yaris", "RAV4"],
    "Honda": ["Civic", "CR-V", "City", "Accord"],
    "Nissan": ["Versa", "Sentra", "March", "Frontier", "Kicks"],
    "Ford": ["Lobo", "Ranger", "Explorer", "Edge"],
    "Chevrolet": ["Aveo", "Onix", "Silverado", "Trax"],
    "VW": ["Jetta", "Vento", "Tiguan", "Polo"],
    "Mazda": ["Mazda 3", "CX-5", "CX-30"],
    "BMW": ["Serie 3", "X3", "X5"],
    "Audi": ["A3", "A4", "Q5"]
}

NOMBRES = ["Juan", "Maria", "Pedro", "Ana", "Luis", "Elena", "Carlos", "Sofia", "Miguel", "Lucia"]
APELLIDOS = ["Garcia", "Martinez", "Lopez", "Sanchez", "Perez", "Gomez", "Rodriguez", "Hernandez"]

SERVICIOS = [
    "Afinación Mayor", "Cambio de Aceite", "Frenos", "Suspensión", 
    "Diagnóstico Eléctrico", "Aire Acondicionado", "Lavado de Motor"
]

def get_next_folio(tipo):
    result = db.folios.find_one_and_update(
        {"tipo": tipo},
        {"$inc": {"secuencia": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER
    )
    secuencia = result.get('secuencia', 1)
    return f"{tipo.upper()}-{str(secuencia).zfill(4)}"

def seed():
    print(f"Iniciando seeding para tenant: {TENANT_ID}...")
    
    # 1. Obtener Sucursales
    sucursales = list(db.sucursales.find())
    if not sucursales:
        print("No hay sucursales. Creando dos sucursales de prueba...")
        s1 = db.sucursales.insert_one({
            "nombre": "Matriz Norte", "direccion": "Av. Independencia 123", 
            "telefono": "4491112233", "responsable": "Gerente Norte", "activa": True,
            "tenant_id": TENANT_ID
        })
        s2 = db.sucursales.insert_one({
            "nombre": "Sucursal Sur", "direccion": "Av. Convención 456", 
            "telefono": "4494445566", "responsable": "Gerente Sur", "activa": True,
            "tenant_id": TENANT_ID
        })
        sucursales = [db.sucursales.find_one({"_id": s1.inserted_id}), db.sucursales.find_one({"_id": s2.inserted_id})]

    # 2. Clientes y Vehículos
    print("Generando clientes y vehículos...")
    client_ids = []
    for _ in range(20):
        nombre = random.choice(NOMBRES)
        ape_p = random.choice(APELLIDOS)
        ape_m = random.choice(APELLIDOS)
        cliente = {
            "nombre": nombre, "apellido_paterno": ape_p, "apellido_materno": ape_m,
            "telefono": f"449{random.randint(1000000, 9999999)}",
            "email": f"{nombre.lower()}.{ape_p.lower()}@gmail.com",
            "tenant_id": TENANT_ID,
            "sucursal_id": str(random.choice(sucursales)["_id"]),
            "createdAt": datetime.utcnow().isoformat()
        }
        c_res = db.clientes.insert_one(cliente)
        c_id = c_res.inserted_id
        client_ids.append(c_id)

        # Vehículo para el cliente
        marca = random.choice(MARCAS)
        modelo = random.choice(MODELOS[marca])
        vehiculo = {
            "cliente_id": str(c_id),
            "tenant_id": TENANT_ID,
            "marca": marca, "modelo": modelo,
            "anio": random.randint(2010, 2024),
            "placas": f"{chr(random.randint(65, 90))}{chr(random.randint(65, 90))}{chr(random.randint(65, 90))}{random.randint(1000, 9999)}",
            "color": random.choice(["Blanco", "Negro", "Gris", "Rojo", "Azul"]),
            "vin": str(uuid.uuid4()).upper()[:17],
            "createdAt": datetime.utcnow()
        }
        db.vehiculos.insert_one(vehiculo)

    # 3. Inventario
    print("Generando inventario...")
    for i in range(30):
        db.items.insert_one({
            "nombre": f"Producto/Servicio {i}",
            "no_parte": f"PART-{1000+i}",
            "marca": random.choice(MARCAS),
            "stock": random.randint(5, 50),
            "precio_compra": random.randint(100, 2000),
            "precio_venta": random.randint(300, 5000),
            "tipo": random.choice(["PRODUCTO", "SERVICIO"]),
            "tenant_id": TENANT_ID,
            "sucursal_id": str(random.choice(sucursales)["_id"]),
            "activo": True
        })

    # 4. Citas y Órdenes
    print("Generando citas y órdenes...")
    for _ in range(15):
        c_id = random.choice(client_ids)
        cliente = db.clientes.find_one({"_id": c_id})
        vehiculo = db.vehiculos.find_one({"cliente_id": str(c_id)})
        sucursal = random.choice(sucursales)
        
        fecha = datetime.utcnow() + timedelta(days=random.randint(-10, 10))
        cita = {
            "clienteId": str(c_id),
            "clienteNombre": f"{cliente['nombre']} {cliente['apellido_paterno']}",
            "vehiculoId": str(vehiculo["_id"]) if vehiculo else None,
            "vehiculoDesc": f"{vehiculo['marca']} {vehiculo['modelo']}" if vehiculo else "",
            "fecha": fecha.strftime("%Y-%m-%d"),
            "horaInicio": "09:00",
            "servicio": random.choice(SERVICIOS),
            "estado": random.choice(["pendiente", "confirmada", "completada"]),
            "tenant_id": TENANT_ID,
            "sucursal_id": str(sucursal["_id"])
        }
        cita_res = db.citas.insert_one(cita)

        # Orden de Servicio para algunas citas
        if random.random() > 0.3:
            folio = get_next_folio("os")
            os = {
                "folio": folio,
                "tenant_id": TENANT_ID,
                "sucursal_id": str(sucursal["_id"]),
                "estado": random.choice(["RECEPCION", "EN_PROCESO", "FINALIZADO"]),
                "cliente_snapshot": {
                    "id": str(c_id), "nombre": cliente["nombre"], 
                    "apellido_paterno": cliente["apellido_paterno"], "telefono": cliente["telefono"]
                },
                "vehiculo_id": str(vehiculo["_id"]) if vehiculo else None,
                "vehiculo_snapshot": {
                    "marca": vehiculo["marca"], "modelo": vehiculo["modelo"], 
                    "placas": vehiculo["placas"], "anio": vehiculo["anio"]
                } if vehiculo else None,
                "cita_id": str(cita_res.inserted_id),
                "puntosArreglar": [{"nombre": cita["servicio"], "items": []}],
                "total": random.randint(1000, 10000),
                "createdAt": fecha,
                "updatedAt": fecha
            }
            db.ordenes_servicio.insert_one(os)

    print("¡Seeding completado con éxito!")

if __name__ == "__main__":
    seed()
