import os
import random
import uuid
from datetime import datetime, timedelta
from pymongo import MongoClient, ReturnDocument
from bson import ObjectId
from dotenv import load_dotenv

# Intentar cargar variables desde .env
load_dotenv()

# Configuración de Conexión
MONGO_USER = os.environ.get("MONGO_USER")
MONGO_PASSWORD = os.environ.get("MONGO_PASSWORD")
MONGO_HOST = os.environ.get("MONGO_HOST")
MONGO_DB = os.environ.get("MONGO_DB", "siga")

# Tenant ID por defecto para la demo (sacado de scripts anteriores)
TENANT_ID = "6fb63f90-8e1e-4b6a-9f4a-8f8f8f8f8f8f"

if not all([MONGO_USER, MONGO_PASSWORD, MONGO_HOST]):
    print("❌ ERROR: Faltan variables de entorno (MONGO_USER, MONGO_PASSWORD o MONGO_HOST).")
    print("Asegúrate de tener un archivo .env o de haberlas exportado en tu terminal.")
    exit(1)

uri = f"mongodb+srv://{MONGO_USER}:{MONGO_PASSWORD}@{MONGO_HOST}/{MONGO_DB}?retryWrites=true&w=majority"
client = MongoClient(uri)
safe_tenant_id = TENANT_ID.replace("-", "")
db = client[f"t_{safe_tenant_id}"]

# DATA SETS
MARCAS = ["Toyota", "Honda", "Nissan", "Ford", "Chevrolet", "VW", "Mazda", "BMW", "Audi", "Mercedes-Benz", "Kia", "Hyundai"]
MODELOS = {
    "Toyota": ["Corolla", "Hilux", "Tacoma", "Yaris", "RAV4", "Camry"],
    "Honda": ["Civic", "CR-V", "City", "Accord", "HR-V"],
    "Nissan": ["Versa", "Sentra", "March", "Frontier", "Kicks", "X-Trail"],
    "Ford": ["Lobo", "Ranger", "Explorer", "Edge", "Mustang", "Figo"],
    "Chevrolet": ["Aveo", "Onix", "Silverado", "Trax", "Captiva", "Suburban"],
    "VW": ["Jetta", "Vento", "Tiguan", "Polo", "Taos", "Golf"],
    "Mazda": ["Mazda 3", "CX-5", "CX-30", "Mazda 2", "CX-9"],
    "BMW": ["Serie 3", "X3", "X5", "Serie 1", "X1"],
    "Audi": ["A3", "A4", "Q5", "Q3", "A5"],
    "Mercedes-Benz": ["Clase C", "GLC", "Clase A", "GLE"],
    "Kia": ["Rio", "Sportage", "Seltos", "Forte"],
    "Hyundai": ["Accent", "Tucson", "Creta", "Elantra"]
}

NOMBRES = ["Juan", "Maria", "Pedro", "Ana", "Luis", "Elena", "Carlos", "Sofia", "Miguel", "Lucia", "Jorge", "Patricia", "Roberto", "Isabel", "Fernando"]
APELLIDOS = ["Garcia", "Martinez", "Lopez", "Sanchez", "Perez", "Gomez", "Rodriguez", "Hernandez", "Jimenez", "Diaz", "Moreno", "Muñoz", "Alvarez"]

PRODUCTOS = [
    {"n": "Aceite Sintético 5W30", "c": 120, "v": 250, "cat": "Lubricantes"},
    {"n": "Filtro de Aceite Universal", "c": 50, "v": 110, "cat": "Filtros"},
    {"n": "Batería LTH 12V", "c": 1200, "v": 2400, "cat": "Eléctrico"},
    {"n": "Balatas Delanteras Cerámica", "c": 450, "v": 950, "cat": "Frenos"},
    {"n": "Bujía Iridium", "c": 80, "v": 220, "cat": "Encendido"},
    {"n": "Filtro de Aire Motor", "c": 110, "v": 320, "cat": "Filtros"},
    {"n": "Amortiguador Delantero", "c": 800, "v": 1650, "cat": "Suspensión"},
    {"n": "Refrigerante Rosa 1L", "c": 60, "v": 140, "cat": "Líquidos"},
    {"n": "Limpiaparabrisas 22\"", "c": 70, "v": 180, "cat": "Accesorios"},
    {"n": "Kit Afinación Mayor", "c": 1500, "v": 3200, "cat": "Servicios"}
]

SERVICIOS = [
    "Afinación Mayor", "Cambio de Aceite", "Frenos", "Suspensión", 
    "Diagnóstico Eléctrico", "Aire Acondicionado", "Lavado de Motor",
    "Alineación y Balanceo", "Reparación de Transmisión", "Hojalatería y Pintura"
]

ESTADOS_OS = ["RECEPCION", "COTIZADO", "APROBADO", "EN_PROCESO", "FINALIZADO", "ENTREGADO", "CANCELADO"]

def get_next_folio(tipo):
    result = db.folios.find_one_and_update(
        {"tipo": tipo},
        {"$inc": {"secuencia": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER
    )
    secuencia = result.get('secuencia', 1)
    return f"{tipo.upper()}-{str(secuencia).zfill(4)}"

def seed_mega():
    print("--- INICIANDO MEGA SEED ULTRA-INTELIGENTE ---")
    
    target_db = None
    target_tenant_id = TENANT_ID
    
    print("Buscando sucursales o usuarios conocidos para auto-detección...")
    all_dbs = [d for d in client.list_database_names() if d.startswith("t_")]
    
    # Prioridad 1: Buscar por email del usuario omar.valdez.becerril@gmail.com
    for db_name in all_dbs:
        test_db = client[db_name]
        u = test_db.usuarios.find_one({"email": "omar.valdez.becerril@gmail.com"})
        if u:
            target_db = test_db
            target_tenant_id = u.get('tenant_id', target_tenant_id)
            print(f"¡Usuario detectado en {db_name}! Usando este Tenant.")
            break
            
    # Prioridad 2: Buscar por nombre de sucursal (Regex)
    if not target_db:
        for db_name in all_dbs:
            test_db = client[db_name]
            s = test_db.sucursales.find_one({"nombre": {"$regex": "Santa Fe|Lomas|Metepec", "$options": "i"}})
            if s:
                target_db = test_db
                target_tenant_id = s.get('tenant_id', s.get('tenantId', TENANT_ID))
                print(f"¡Sucursal detectada en {db_name}! Usando este Tenant.")
                break
    
    if not target_db:
        print(f"CRÍTICO: No se detectó tu cuenta. Usando fallback forzado: t_{TENANT_ID.replace('-','')}")
        target_db = db
    
    db_ctx = target_db

    # 1. Sucursales (Asegurar 3 con nombres correctos)
    sucs = list(db_ctx.sucursales.find())
    print(f"Sucursales encontradas: {len(sucs)}")
    nombres_reales = ["Santa Fe Verde", "Lomas Verdes", "Metepec"]
    
    if len(sucs) < 3:
        for i in range(len(sucs), 3):
            db_ctx.sucursales.insert_one({
                "nombre": nombres_reales[i], 
                "direccion": f"Avenida Principal # {random.randint(10,5000)}",
                "telefono": f"55{random.randint(1112233, 9998877)}", 
                "responsable": f"Gerente {nombres_reales[i]}",
                "activa": True, "tenant_id": target_tenant_id
            })
        sucs = list(db_ctx.sucursales.find())
    
    suc_ids = [str(s["_id"]) for s in sucs]

    # 2. Usuarios Mock
    print("Generando 20 usuarios mock...")
    roles = ["ADMIN", "ASESOR", "MECANICO"]
    for i in range(20):
        rol = random.choice(roles)
        nombre = random.choice(NOMBRES)
        ape = random.choice(APELLIDOS)
        email = f"{nombre.lower()}.{ape.lower()}{i}@demo.com"
        db_ctx.usuarios.update_one(
            {"email": email},
            {"$set": {
                "nombre": nombre, "apellido": ape, "email": email, "rol": rol, "grupo": rol,
                "sucursal_id": random.choice(suc_ids), "activo": True, "tenant_id": target_tenant_id,
                "createdAt": datetime.utcnow()
            }},
            upsert=True
        )

    mecanicos = list(db_ctx.usuarios.find({"grupo": "MECANICO"}))
    mecanico_ids = [str(m["_id"]) for m in mecanicos]

    # 3. Inventario Masivo (200 items por sucursal)
    print("Generando inventario masivo...")
    for suc_id in suc_ids:
        # Verificar si ya hay suficiente inventario
        actual = db_ctx.items.count_documents({"sucursal_id": suc_id})
        if actual >= 150:
            print(f"Sucursal {suc_id} ya tiene {actual} items. Saltando...")
            continue
            
        for i in range(200):
            base = random.choice(PRODUCTOS)
            nombre_item = f"{base['n']} - {random.choice(MARCAS)}"
            db_ctx.items.insert_one({
                "nombre": nombre_item,
                "no_parte": f"NP-{suc_id[:4]}-{random.randint(10000, 99999)}",
                "marca": random.choice(MARCAS),
                "categoria": base['cat'],
                "stock": random.randint(10, 100),
                "precio_compra": base['c'],
                "precio_venta": base['v'],
                "precio_taller": base['v'] * 0.9,
                "precio_cliente": base['v'] * 0.95,
                "tipo": "PRODUCTO" if "Servicio" not in base['n'] else "SERVICIO",
                "tenant_id": target_tenant_id, "sucursal_id": suc_id, "activo": True
            })

    # 4. Clientes y Vehículos (50)
    print("Generando 50 clientes y vehículos...")
    client_ids = []
    for i in range(50):
        nombre = random.choice(NOMBRES)
        ape_p = random.choice(APELLIDOS)
        cliente = {
            "nombre": nombre, "apellido_paterno": ape_p, "apellido_materno": random.choice(APELLIDOS),
            "telefono": f"44{random.randint(10,99)}{random.randint(1000000, 9999999)}",
            "email": f"{nombre.lower()}.{ape_p.lower()}{i}@gmail.com",
            "tenant_id": target_tenant_id, "sucursal_id": random.choice(suc_ids), "createdAt": datetime.utcnow().isoformat()
        }
        c_res = db_ctx.clientes.insert_one(cliente)
        cid = c_res.inserted_id
        client_ids.append(cid)

        # 1 o 2 vehículos por cliente
        for _ in range(random.randint(1, 2)):
            marca = random.choice(MARCAS)
            vehiculo = {
                "cliente_id": str(cid), "tenant_id": target_tenant_id,
                "marca": marca, "modelo": random.choice(MODELOS[marca]),
                "anio": random.randint(2005, 2025), "color": random.choice(["Rojo", "Azul", "Gris", "Blanco", "Negro", "Plata"]),
                "placas": f"{chr(random.randint(65, 90))}{random.randint(100, 999)}{chr(random.randint(65, 90))}",
                "vin": str(uuid.uuid4()).upper()[:17], "createdAt": datetime.utcnow()
            }
            db_ctx.vehiculos.insert_one(vehiculo)

    # 5. Citas (300)
    print("Generando 300 citas...")
    for i in range(300):
        c_id = random.choice(client_ids)
        cliente = db_ctx.clientes.find_one({"_id": c_id})
        vehiculo = db_ctx.vehiculos.find_one({"cliente_id": str(c_id)})
        suc_id = random.choice(suc_ids)
        
        dias_offset = random.randint(-30, 30)
        fecha = datetime.utcnow() + timedelta(days=dias_offset)
        
        cita = {
            "clienteId": str(c_id),
            "clienteNombre": f"{cliente['nombre']} {cliente['apellido_paterno']}",
            "vehiculoId": str(vehiculo["_id"]) if vehiculo else None,
            "vehiculoDesc": f"{vehiculo['marca']} {vehiculo['modelo']}" if vehiculo else "Sin Vehículo",
            "fecha": fecha.strftime("%Y-%m-%d"),
            "horaInicio": f"{random.randint(8, 18):02d}:00",
            "servicio": random.choice(SERVICIOS),
            "estado": random.choice(["pendiente", "confirmada", "completada", "cancelada"]),
            "tenant_id": target_tenant_id, "sucursal_id": suc_id
        }
        cita_res = db_ctx.citas.insert_one(cita)

        # 6. Órdenes de Servicio (para el 70% de las citas no canceladas)
        if cita["estado"] != "cancelada" and random.random() < 0.7:
            # Reemplazar get_next_folio para que use db_ctx
            res_folio = db_ctx.folios.find_one_and_update(
                {"tipo": "os"}, {"$inc": {"secuencia": 1}},
                upsert=True, return_document=ReturnDocument.AFTER
            )
            folio = f"OS-{str(res_folio.get('secuencia', 1)).zfill(4)}"
            
            estado = random.choice(ESTADOS_OS)
            
            puntos = []
            num_puntos = random.randint(1, 3)
            total_os = 0
            for p_idx in range(num_puntos):
                items_punto = []
                for _ in range(random.randint(0, 3)):
                    inv_item = db_ctx.items.find_one({"sucursal_id": suc_id, "tipo": "PRODUCTO"})
                    if inv_item:
                        qty = random.randint(1, 4)
                        sub = qty * inv_item["precio_venta"]
                        total_os += sub
                        items_punto.append({
                            "item_id": str(inv_item["_id"]), "nombre": inv_item["nombre"],
                            "piezas": qty, "precioVenta": inv_item["precio_venta"], "subtotal": sub
                        })
                puntos.append({"nombre": f"Punto de Revisión {p_idx+1}", "items": items_punto})

            os_doc = {
                "folio": folio, "tenant_id": target_tenant_id, "sucursal_id": suc_id, "estado": estado,
                "cliente_id": str(c_id),
                "cliente_snapshot": {
                    "id": str(c_id), "nombre": cliente["nombre"], 
                    "apellido_paterno": cliente["apellido_paterno"], "telefono": cliente["telefono"]
                },
                "vehiculo_id": str(vehiculo["_id"]) if vehiculo else None,
                "vehiculo_snapshot": {
                    "marca": vehiculo["marca"], "modelo": vehiculo["modelo"], 
                    "placas": vehiculo["placas"], "anio": vehiculo["anio"], "color": vehiculo.get("color", "N/A")
                } if vehiculo else None,
                "cita_id": str(cita_res.inserted_id),
                "mecanico_id": random.choice(mecanico_ids) if mecanico_ids else None,
                "puntosArreglar": puntos, "total": total_os, "anticipo": random.randint(0, 500),
                "falla_reportada": "Revisión general y " + cita["servicio"],
                "createdAt": fecha.isoformat(), "updatedAt": fecha.isoformat()
            }
            db_ctx.ordenes_servicio.insert_one(os_doc)

    # 7. Ventas de Mostrador (POS) - 250 ventas totales
    print("Generando 250 ventas POS...")
    for _ in range(250):
        suc_id = random.choice(suc_ids)
        fecha_venta = datetime.utcnow() - timedelta(days=random.randint(0, 45))
        
        res_folio_v = db_ctx.folios.find_one_and_update(
            {"tipo": "v"}, {"$inc": {"secuencia": 1}},
            upsert=True, return_document=ReturnDocument.AFTER
        )
        folio_v = f"V-{str(res_folio_v.get('secuencia', 1)).zfill(4)}"
        
        items_venta = []
        total_v = 0
        for _ in range(random.randint(1, 5)):
            inv_item = db_ctx.items.find_one({"sucursal_id": suc_id, "tipo": "PRODUCTO"})
            if inv_item:
                qty = random.randint(1, 3)
                sub = qty * inv_item["precio_venta"]
                total_v += sub
                items_venta.append({
                    "id": str(inv_item["_id"]), "nombre": inv_item["nombre"],
                    "cantidad": qty, "precio": inv_item["precio_venta"], "subtotal": sub
                })
        
        venta = {
            "folio": folio_v, "tenant_id": target_tenant_id, "sucursal_id": suc_id,
            "items": items_venta, "total": total_v, "metodo_pago": random.choice(["EFECTIVO", "TARJETA", "TRANSFERENCIA"]),
            "cliente_id": str(random.choice(client_ids)), "createdAt": fecha_venta.isoformat()
        }
        db_ctx.ventas.insert_one(venta)

    print(f"--- MEGA SEEDING COMPLETO ---")
    print(f"Sucursales: {len(sucs)}")
    print(f"Clientes/Vehículos: 50")
    print(f"Citas: 300")
    print(f"Ventas POS: 250")
    print(f"Inventario: ~600 items")
    print(f"Usuarios: 20")

if __name__ == "__main__":
    seed_mega()
