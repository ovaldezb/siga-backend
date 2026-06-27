import requests
import boto3
import json
import random
import uuid
from datetime import datetime, timedelta

# CONFIG
API_URL = "https://4a3ldlf8y9.execute-api.us-east-1.amazonaws.com/dev"
USER_POOL_ID = "us-east-1_e70nKlh9j"
CLIENT_ID = "7399e4a1615fm4vlk40p46na68"
REGION = "us-east-1"
USER_EMAIL = "omar.valdez.becerril@gmail.com"
USER_PASS = "@1q2w3e4r5T"

class SIGAPopulator:
    def __init__(self):
        self.token = None
        self.headers = {}
        self.client = boto3.client('cognito-idp', region_name=REGION)
        self.branches = []
        self.customers = []
        self.items = []

    def login(self):
        print(f"[AUTH] Logging in as {USER_EMAIL}...")
        try:
            response = self.client.admin_initiate_auth(
                UserPoolId=USER_POOL_ID,
                ClientId=CLIENT_ID,
                AuthFlow='ADMIN_NO_SRP_AUTH',
                AuthParameters={'USERNAME': USER_EMAIL, 'PASSWORD': USER_PASS}
            )
            self.token = response['AuthenticationResult']['IdToken']
            self.headers = {'Authorization': self.token}
            print("  Login Successful.")
            return True
        except Exception as e:
            print(f"  Login Failed: {str(e)}")
            return False

    def get_data(self):
        print("[DATA] Fetching branches and customers...")
        res_sucs = requests.get(f"{API_URL}/sucursales", headers=self.headers)
        self.branches = res_sucs.json().get('data', [])
        
        res_clis = requests.get(f"{API_URL}/clientes?limit=100", headers=self.headers)
        self.customers = res_clis.json().get('data', {}).get('items', [])
        
        print(f"  Found {len(self.branches)} branches and {len(self.customers)} customers.")

    def create_items(self):
        print("[INVENTORY] Creating 20 items per branch...")
        categories = ["Frenos", "Suspensión", "Motor", "Eléctrico", "Líquidos", "Filtros"]
        icons = ["ri-settings-3-line", "ri-car-line", "ri-flashlight-line", "ri-drop-line", "ri-filter-line"]
        brands = ["Bosch", "Brembo", "Mobil1", "Castrol", "ACDelco", "NGK"]
        
        for branch in self.branches:
            bid = branch['id']
            print(f"  Processing branch: {branch['nombre']} ({bid})")
            for i in range(20):
                brand = random.choice(brands)
                name = f"{random.choice(['Balatas', 'Amortiguador', 'Bujía', 'Filtro', 'Aceite', 'Disco'])} {brand} Spec-{i}"
                item_data = {
                    "nombre": name,
                    "no_parte": f"NP-{bid[:4]}-{random.randint(1000, 9999)}-{i}",
                    "tipo": "PRODUCTO",
                    "precio_venta": random.randint(200, 5000),
                    "sucursalId": bid,
                    "categoria": random.choice(categories),
                    "marca": brand,
                    "icon": random.choice(icons),
                    "stock": random.randint(5, 50)
                }
                res = requests.post(f"{API_URL}/items", json=item_data, headers=self.headers)
                if res.status_code == 201:
                    self.items.append(res.json()['data'])
        print(f"  Total items created: {len(self.items)}")

    def create_vehicles(self):
        print("[VEHICLES] Creating 20 vehicles...")
        marcas = ["Toyota", "Honda", "Ford", "VW", "Nissan", "BMW"]
        for i in range(20):
            customer = random.choice(self.customers)
            branch = random.choice(self.branches)
            marca = random.choice(marcas)
            v_data = {
                "marca": marca,
                "modelo": f"Model {random.choice(['X', 'Y', 'Z', 'Prime', 'Pro'])}",
                "placas": f"ABC-{random.randint(100, 999)}-{i}",
                "cliente_id": customer['id'],
                "sucursalId": branch['id'],
                "anio": random.randint(2010, 2024),
                "color": random.choice(["Rojo", "Azul", "Gris", "Blanco", "Negro"])
            }
            requests.post(f"{API_URL}/vehiculos", json=v_data, headers=self.headers)
        print("  20 vehicles created.")

    def create_appointments(self):
        print("[CITAS] Creating 10 appointments...")
        servicios = ["Cambio de Aceite", "Frenos", "Afinación", "Diagnóstico", "Suspensión"]
        for i in range(10):
            customer = random.choice(self.customers)
            branch = random.choice(self.branches)
            date = (datetime.now() + timedelta(days=random.randint(1, 15))).strftime("%Y-%m-%d")
            c_data = {
                "clienteId": customer['id'],
                "clienteNombre": f"{customer['nombre']} {customer['apellido_paterno']}",
                "sucursal_id": branch['id'],
                "fecha": date,
                "horaInicio": f"{random.randint(9, 17):02d}:00",
                "servicio": random.choice(servicios),
                "estado": "pendiente"
            }
            requests.post(f"{API_URL}/citas", json=c_data, headers=self.headers)
        print("  10 appointments created.")

    def create_orders(self):
        print("[ORDENES] Creating 20 service orders...")
        estados = ["RECEPCION", "COTIZADO", "APROBADO", "EN_PROCESO", "FINALIZADO"]
        # Fetch fresh vehicles
        res_v = requests.get(f"{API_URL}/vehiculos?limit=100", headers=self.headers)
        vehicles = res_v.json().get('data', {}).get('items', [])
        
        if not vehicles:
            print("  No vehicles found to create orders.")
            return

        for i in range(20):
            vehicle = random.choice(vehicles)
            branch_id = vehicle.get('sucursalId') or random.choice(self.branches)['id']
            
            # Fetch customer info
            cid = vehicle['cliente_id']
            res_c = requests.get(f"{API_URL}/clientes/{cid}", headers=self.headers)
            customer = res_c.json().get('data', {})
            
            # Prepare snapshot
            c_snapshot = {
                "id": cid,
                "nombre": customer.get('nombre'),
                "apellido_paterno": customer.get('apellido_paterno'),
                "telefono": customer.get('telefono')
            }
            
            v_snapshot = {
                "marca": vehicle.get('marca'),
                "modelo": vehicle.get('modelo'),
                "placas": vehicle.get('placas'),
                "anio": vehicle.get('anio'),
                "color": vehicle.get('color')
            }
            
            # Get next folio (via API)
            res_f = requests.get(f"{API_URL}/folios/os?sucursalId={branch_id}", headers=self.headers)
            folio = res_f.json().get('data', {}).get('folio', f"OS-TMP-{i}")
            
            order_data = {
                "folio": folio,
                "sucursalId": branch_id,
                "estado": random.choice(estados),
                "cliente_snapshot": c_snapshot,
                "vehiculo_id": vehicle['id'],
                "vehiculo_snapshot": v_snapshot,
                "falla_reportada": "Ruido en suspensión y revisión general.",
                "diagnostico": "Se requiere cambio de componentes preventivo.",
                "total": random.randint(1500, 15000),
                "anticipo": random.randint(0, 500)
            }
            requests.post(f"{API_URL}/ordenes", json=order_data, headers=self.headers)
        print("  20 service orders created.")

    def run(self):
        if not self.login(): return
        self.get_data()
        self.create_items()
        self.create_vehicles()
        self.create_appointments()
        self.create_orders()
        print("\n[FINISH] All data generated successfully.")

if __name__ == "__main__":
    SIGAPopulator().run()
