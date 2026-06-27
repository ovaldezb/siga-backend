import os
import sys
import json
from dotenv import load_dotenv

# Asegura import desde la raíz del proyecto 2
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

load_dotenv()

from src.shared.infrastructure.database import get_platform_db
from src.handlers.cotizaciones.cotizaciones_manager import list_cotizaciones_handler

def simulate_handler():
    plat = get_platform_db()
    tenant = plat.talleres.find_one({}, {"tenantId": 1, "_id": 0})
    if not tenant:
        print("No se encontraron tenants.")
        return

    tenant_id = tenant.get("tenantId")
    print(f"Simulando list_cotizaciones_handler para el tenant: {tenant_id}")

    # Simulando el evento de AWS API Gateway con el tenant_id en los claims de Cognito
    event = {
        "requestContext": {
            "authorizer": {
                "claims": {
                    "custom:tenant_id": tenant_id,
                    "cognito:groups": "ADMIN",
                    "email": "test@siga.com"
                }
            }
        },
        "queryStringParameters": {
            "limit": "300"
        }
    }

    class MockContext:
        function_name = "list_cotizaciones"
        memory_limit_in_mb = "256"
        invoked_function_arn = "arn:aws:lambda:us-east-1:123456789012:function:list_cotizaciones"
        aws_request_id = "request-id-123"

    # Llamar al handler
    response = list_cotizaciones_handler(event, MockContext())
    
    print(f"Status Code: {response.get('statusCode')}")
    body = json.loads(response.get("body", "{}"))
    print(f"Mensaje del body: {body.get('message')}")
    data = body.get("data", [])
    print(f"Total cotizaciones devueltas: {len(data)}")
    if data:
        print("Primeras 3 devueltas:")
        for doc in data[:3]:
            print(f"  - Folio: {doc.get('folio')}, Nombre: {doc.get('nombre')}, Tipo: {doc.get('tipo')}")

if __name__ == "__main__":
    simulate_handler()
