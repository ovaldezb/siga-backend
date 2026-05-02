import json
import uuid
from datetime import datetime
from aws_lambda_powertools import Logger
from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.infrastructure.database import get_tenant_db
from bson import ObjectId

logger = Logger()

def list_clientes_handler(event, context):
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        query_params = event.get('queryStringParameters') or {}
        search_query = query_params.get('q', '').strip()
        
        db = get_tenant_db(tenant_id)
        
        filter_query = {}
        if search_query:
            import re
            regex = re.compile(re.escape(search_query), re.IGNORECASE)
            filter_query = {
                "$or": [
                    {"nombre": regex},
                    {"apellido_paterno": regex},
                    {"telefono": regex}
                ]
            }

        clientes = list(db.clientes.find(filter_query).limit(20))
        
        # Formatear para JSON
        for c in clientes:
            c['id'] = str(c.pop('_id'))
            
        return create_response(200, "Clientes obtenidos", clientes)
    except Exception as e:
        return handle_exception(e)

def create_cliente_handler(event, context):
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        body = json.loads(event.get('body', '{}'))
        
        # Validación básica manual para no sobrecomplicar el handler por ahora
        if not body.get('nombre') or not body.get('telefono'):
            return create_response(400, "Nombre y teléfono son requeridos.")

        db = get_tenant_db(tenant_id)
        
        nuevo_cliente = {
            "nombre": body['nombre'],
            "apellido_paterno": body['apellido_paterno'],
            "apellido_materno": body.get('apellido_materno'),
            "telefono": body['telefono'],
            "email": body.get('email'),
            "direccion": body.get('direccion'),
            "createdAt": datetime.utcnow().isoformat(),
            "tenant_id": tenant_id
        }
        
        result = db.clientes.insert_one(nuevo_cliente)
        nuevo_cliente['id'] = str(result.inserted_id)
        del nuevo_cliente['_id']
        
        return create_response(201, "Cliente creado exitosamente", nuevo_cliente)
    except Exception as e:
        return handle_exception(e)

def get_cliente_handler(event, context):
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        cliente_id = event['pathParameters']['id']
        
        db = get_tenant_db(tenant_id)
        cliente = db.clientes.find_one({"_id": ObjectId(cliente_id)})
        
        if not cliente:
            return create_response(404, "Cliente no encontrado.")
            
        cliente['id'] = str(cliente.pop('_id'))
        return create_response(200, "Detalle del cliente", cliente)
    except Exception as e:
        return handle_exception(e)

def add_vehiculo_handler(event, context):
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        cliente_id = event['pathParameters']['id']
        
        body = json.loads(event.get('body', '{}'))
        
        db = get_tenant_db(tenant_id)
        
        # Verificar que el cliente existe
        cliente = db.clientes.find_one({"_id": ObjectId(cliente_id)})
        if not cliente:
            return create_response(404, "Cliente no encontrado.")

        nuevo_vehiculo = {
            "vehiculo_id": str(uuid.uuid4()),
            "cliente_id": cliente_id,
            "placas": body['placas'],
            "marca": body['marca'],
            "modelo": body['modelo'],
            "año": body.get('año') or body.get('anio'),
            "vin": body.get('vin'),
            "tenant_id": tenant_id,
            "createdAt": datetime.utcnow().isoformat()
        }
        
        if '_id' in nuevo_vehiculo: del nuevo_vehiculo['_id']
        
        return create_response(201, "Vehículo registrado correctamente", nuevo_vehiculo)
    except Exception as e:
        return handle_exception(e)
