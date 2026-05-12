import json
import uuid
from datetime import datetime
from aws_lambda_powertools import Logger
from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.utils.auth_utils import parse_object_id
from src.shared.infrastructure.database import get_tenant_db
from bson import ObjectId
from pymongo import ReturnDocument

logger = Logger()

ALLOWED_UPDATE_FIELDS = {
    "nombre", "apellido_paterno", "apellido_materno", "telefono", "email",
    "rfc", "razon_social", "regimen_fiscal", "codigo_postal", "tipo_persona",
    "limite_credito", "dias_credito", "nivel_precio", "sucursal_id"
}

def list_clientes_handler(event, context):
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        query_params = event.get('queryStringParameters') or {}
        search_query = query_params.get('q', '').strip()
        sucursal_id = query_params.get('sucursalId') or query_params.get('sucursal_id')
        page = int(query_params.get('page', 1))
        limit = int(query_params.get('limit', 20))
        skip = (page - 1) * limit
        
        db = get_tenant_db(tenant_id)
        
        filter_query = {}
        if sucursal_id:
            filter_query["sucursal_id"] = sucursal_id

        if search_query:
            import re
            regex = re.compile(re.escape(search_query), re.IGNORECASE)
            search_filters = [
                {"nombre": regex},
                {"apellido_paterno": regex},
                {"telefono": regex}
            ]
            if filter_query:
                filter_query = {"$and": [filter_query, {"$or": search_filters}]}
            else:
                filter_query = {"$or": search_filters}

        total = db.clientes.count_documents(filter_query)
        clientes = list(db.clientes.find(filter_query).skip(skip).limit(limit))
        
        # Formatear para JSON y preparar IDs para conteo de vehículos
        client_ids = []
        for c in clientes:
            c['id'] = str(c.pop('_id'))
            if 'sucursal_id' in c:
                c['sucursalId'] = c.pop('sucursal_id')
            client_ids.append(c['id'])
        
        # Conteo de vehículos eficiente (una sola consulta para toda la página)
        if client_ids:
            counts = list(db["vehiculos"].aggregate([
                {"$match": {"cliente_id": {"$in": client_ids}}},
                {"$group": {"_id": "$cliente_id", "count": {"$sum": 1}}}
            ]))
            counts_dict = {item['_id']: item['count'] for item in counts}
            for c in clientes:
                c['num_vehiculos'] = counts_dict.get(c['id'], 0)
        else:
            for c in clientes:
                c['num_vehiculos'] = 0
            
        return create_response(200, "Clientes obtenidos", {
            "items": clientes,
            "total": total,
            "page": page,
            "limit": limit,
            "totalPages": (total + limit - 1) // limit if limit > 0 else 0
        })
    except Exception as e:
        return handle_exception(e)

def create_cliente_handler(event, context):
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        body = json.loads(event.get('body', '{}'))
        
        if 'sucursalId' in body and 'sucursal_id' not in body:
            body['sucursal_id'] = body.pop('sucursalId')

        # Validación básica manual para no sobrecomplicar el handler por ahora
        # VALIDACIÓN ESTRICTA
        required = ["nombre", "apellido_paterno", "telefono", "sucursal_id"]
        for field in required:
            if not body.get(field):
                return create_response(400, f"El campo '{field}' es obligatorio.")

        db = get_tenant_db(tenant_id)
        
        nuevo_cliente = {
            "nombre": body['nombre'],
            "apellido_paterno": body['apellido_paterno'],
            "apellido_materno": body.get('apellido_materno', ''),
            "telefono": body['telefono'],
            "email": body.get('email', ''),
            "rfc": body.get('rfc', 'XAXX010101000'),
            "razon_social": body.get('razon_social', ''),
            "regimen_fiscal": body.get('regimen_fiscal', '612'), # Personas Físicas con Actividades Empresariales por defecto
            "codigo_postal": body.get('codigo_postal', ''),
            "tipo_persona": body.get('tipo_persona', 'FISICA'),
            "limite_credito": float(body.get('limite_credito', 0)),
            "dias_credito": int(body.get('dias_credito', 0)),
            "nivel_precio": int(body.get('nivel_precio', 1)),
            "vehiculos_resumen": [],
            "sucursal_id": body['sucursal_id'],
            "createdAt": datetime.utcnow().isoformat(),
            "tenant_id": tenant_id
        }
        
        result = db.clientes.insert_one(nuevo_cliente)
        nuevo_cliente['id'] = str(result.inserted_id)
        del nuevo_cliente['_id']
        if 'sucursal_id' in nuevo_cliente:
            nuevo_cliente['sucursalId'] = nuevo_cliente.pop('sucursal_id')
        
        return create_response(201, "Cliente creado exitosamente", nuevo_cliente)
    except Exception as e:
        return handle_exception(e)

def get_cliente_handler(event, context):
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        cliente_id = event['pathParameters']['id']

        object_id, err = parse_object_id(cliente_id)
        if err:
            return create_response(400, err)

        db = get_tenant_db(tenant_id)
        cliente = db.clientes.find_one({"_id": object_id})

        if not cliente:
            return create_response(404, "Cliente no encontrado.")

        cliente['id'] = str(cliente.pop('_id'))
        if 'sucursal_id' in cliente:
            cliente['sucursalId'] = cliente.pop('sucursal_id')
        return create_response(200, "Detalle del cliente", cliente)
    except Exception as e:
        return handle_exception(e)


def update_cliente_handler(event, context):
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')

        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        cliente_id = event['pathParameters']['id']
        object_id, err = parse_object_id(cliente_id)
        if err:
            return create_response(400, err)

        body = json.loads(event.get('body', '{}'))
        
        # Mapear sucursalId a sucursal_id
        if 'sucursalId' in body:
            body['sucursal_id'] = body.pop('sucursalId')

        update_doc = {k: body[k] for k in ALLOWED_UPDATE_FIELDS if k in body}

        if not update_doc:
            return create_response(400, "No hay campos válidos para actualizar.")

        update_doc['updatedAt'] = datetime.utcnow().isoformat()

        db = get_tenant_db(tenant_id)
        result = db.clientes.find_one_and_update(
            {"_id": object_id},
            {"$set": update_doc},
            return_document=ReturnDocument.AFTER
        )

        if not result:
            return create_response(404, "Cliente no encontrado.")

        result['id'] = str(result.pop('_id'))
        if 'sucursal_id' in result:
            result['sucursalId'] = result.pop('sucursal_id')
        return create_response(200, "Cliente actualizado", result)
    except Exception as e:
        return handle_exception(e)


def delete_cliente_handler(event, context):
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')

        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        cliente_id = event['pathParameters']['id']
        object_id, err = parse_object_id(cliente_id)
        if err:
            return create_response(400, err)

        db = get_tenant_db(tenant_id)

        vehiculos_count = db.vehiculos.count_documents({"cliente_id": cliente_id})
        if vehiculos_count > 0:
            return create_response(
                409,
                f"No se puede eliminar: el cliente tiene {vehiculos_count} vehículo(s) asociado(s)."
            )

        result = db.clientes.delete_one({"_id": object_id})
        if result.deleted_count == 0:
            return create_response(404, "Cliente no encontrado.")

        return create_response(200, "Cliente eliminado")
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

        if 'sucursalId' in body and 'sucursal_id' not in body:
            body['sucursal_id'] = body.pop('sucursalId')

        if not body.get("sucursal_id"):
            return create_response(400, "El campo 'sucursal_id' es obligatorio.")

        nuevo_vehiculo = {
            "vehiculo_id": str(uuid.uuid4()),
            "cliente_id": cliente_id,
            "placas": body['placas'],
            "marca": body['marca'],
            "modelo": body['modelo'],
            "año": body.get('año') or body.get('anio'),
            "vin": body.get('vin'),
            "sucursal_id": body['sucursal_id'],
            "tenant_id": tenant_id,
            "createdAt": datetime.utcnow().isoformat()
        }
        
        # Insertar en colección de vehículos
        result = db.vehiculos.insert_one(nuevo_vehiculo)
        nuevo_vehiculo['id'] = str(result.inserted_id)
        if '_id' in nuevo_vehiculo: del nuevo_vehiculo['_id']
        if 'sucursal_id' in nuevo_vehiculo:
            nuevo_vehiculo['sucursalId'] = nuevo_vehiculo.pop('sucursal_id')
        
        # Actualizar resumen en el cliente
        vehiculo_resumen = {
            "id": nuevo_vehiculo['id'],
            "placas": nuevo_vehiculo['placas'],
            "marca": nuevo_vehiculo['marca'],
            "modelo": nuevo_vehiculo['modelo'],
            "año": nuevo_vehiculo['año']
        }
        db.clientes.update_one(
            {"_id": ObjectId(cliente_id)},
            {"$push": {"vehiculos_resumen": vehiculo_resumen}}
        )
        
        return create_response(201, "Vehículo registrado correctamente", nuevo_vehiculo)
    except Exception as e:
        return handle_exception(e)
