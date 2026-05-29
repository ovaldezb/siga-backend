import os
import sys
from dotenv import load_dotenv

# Asegura import desde la raíz del proyecto
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

load_dotenv()

from src.shared.infrastructure.database import get_platform_db, get_tenant_db

def test_query():
    plat = get_platform_db()
    tenants = [t["tenantId"] for t in plat.talleres.find({}, {"tenantId": 1, "_id": 0}) if t.get("tenantId")]
    
    print(f"Tenants encontrados en la plataforma: {tenants}")
    
    for tenant_id in tenants:
        print(f"\n==================================================")
        print(f"Conectando al tenant: {tenant_id}")
        db = get_tenant_db(tenant_id)
        
        total = db.cotizaciones.count_documents({})
        print(f"Total de cotizaciones en el tenant: {total}")
        
        plantillas_count = db.cotizaciones.count_documents({"tipo": "PLANTILLA"})
        clientes_count = db.cotizaciones.count_documents({"tipo": "CLIENTE"})
        print(f"Total PLANTILLAS: {plantillas_count}")
        print(f"Total CLIENTE: {clientes_count}")
        
        print("Muestra de plantillas:")
        cursor = db.cotizaciones.find({"tipo": "PLANTILLA"}).limit(5)
        for doc in cursor:
            print(f"  - Folio: {doc.get('folio')}, Nombre: {doc.get('nombre')}, Sucursal: {doc.get('sucursal_id')}, Puntos: {len(doc.get('puntosArreglar', []))}")

if __name__ == "__main__":
    test_query()
