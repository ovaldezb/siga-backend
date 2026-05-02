import json
from bson import ObjectId
from datetime import datetime
from aws_lambda_powertools import Logger
from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.infrastructure.database import get_tenant_db

logger = Logger()

@logger.inject_lambda_context
def list_vehiculos_handler(event, context):
    """GET /vehiculos?cliente_id=xxx&page=1&limit=25 — Lista vehículos con paginación."""
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')

        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        query_params = event.get('queryStringParameters') or {}
        cliente_id = query_params.get('cliente_id', '').strip()
        search = query_params.get('search', '').strip()
        
        # Paginación
        page = int(query_params.get('page', 1))
        limit = int(query_params.get('limit', 25))
        skip = (page - 1) * limit

        db = get_tenant_db(tenant_id)

        # Filtro base
        filtro = {}
        if cliente_id:
            filtro["cliente_id"] = cliente_id
            
        if search:
            regex = {"$regex": search, "$options": "i"}
            filtro["$or"] = [
                {"marca": regex},
                {"modelo": regex},
                {"placas": regex}
            ]
        
        # Total de registros para el filtro dado
        total = db["vehiculos"].count_documents(filtro)
        
        # Consulta con skip y limit
        cursor = db["vehiculos"].find(filtro).sort("createdAt", -1).skip(skip).limit(limit)

        vehiculos = []
        for v in cursor:
            v['id'] = str(v.pop('_id'))
            if 'createdAt' in v and isinstance(v['createdAt'], datetime):
                v['createdAt'] = v['createdAt'].isoformat()
            vehiculos.append(v)

        # Respuesta estructurada para paginación
        resultado = {
            "items": vehiculos,
            "total": total,
            "page": page,
            "limit": limit,
            "totalPages": (total + limit - 1) // limit
        }

        return create_response(200, "Vehículos obtenidos", resultado)

    except Exception as e:
        return handle_exception(e)

@logger.inject_lambda_context
def get_vehiculo_handler(event, context):
    """GET /vehiculos/{id}  — Obtiene un vehículo por su _id de MongoDB."""
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')

        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        vehiculo_id = event['pathParameters']['id']
        db = get_tenant_db(tenant_id)

        vehiculo = db["vehiculos"].find_one({"_id": ObjectId(vehiculo_id)})

        if not vehiculo:
            return create_response(404, "Vehículo no encontrado.")

        vehiculo['id'] = str(vehiculo.pop('_id'))
        if 'createdAt' in vehiculo and isinstance(vehiculo['createdAt'], datetime):
            vehiculo['createdAt'] = vehiculo['createdAt'].isoformat()

        return create_response(200, "Vehículo obtenido", vehiculo)

    except Exception as e:
        return handle_exception(e)
