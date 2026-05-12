import json
from bson import ObjectId
from datetime import datetime
from aws_lambda_powertools import Logger
from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.infrastructure.database import get_tenant_db

logger = Logger()

@logger.inject_lambda_context
def list_sucursales_handler(event, context):
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        db = get_tenant_db(tenant_id)
        sucursales = list(db["sucursales"].find())

        for s in sucursales:
            s['id'] = str(s.pop('_id'))
            if 'createdAt' in s and isinstance(s['createdAt'], datetime):
                s['createdAt'] = s['createdAt'].isoformat()
            if 'updatedAt' in s and isinstance(s['updatedAt'], datetime):
                s['updatedAt'] = s['updatedAt'].isoformat()

        return create_response(200, "Sucursales obtenidas", sucursales)
    except Exception as e:
        return handle_exception(e)

@logger.inject_lambda_context
def create_sucursal_handler(event, context):
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        body = json.loads(event.get('body', '{}'))
        db = get_tenant_db(tenant_id)

        nueva_sucursal = {
            "nombre": body.get("nombre"),
            "direccion": body.get("direccion"),
            "telefono": body.get("telefono"),
            "responsable": body.get("responsable"),
            "activa": body.get("activa", True),
            "tenant_id": tenant_id,
            "createdAt": datetime.utcnow(),
            "updatedAt": datetime.utcnow()
        }

        result = db["sucursales"].insert_one(nueva_sucursal)
        sid = str(result.inserted_id)
        
        # Inicializar contador de folios para esta sucursal
        db["folios"].insert_one({
            "tipo": "os",
            "secuencia": 0, # Empezamos en 0 para que el primer next sea 1
            "sucursal_id": sid
        })

        nueva_sucursal['id'] = sid
        del nueva_sucursal['_id']
        nueva_sucursal['createdAt'] = nueva_sucursal['createdAt'].isoformat()
        nueva_sucursal['updatedAt'] = nueva_sucursal['updatedAt'].isoformat()

        return create_response(201, "Sucursal creada", nueva_sucursal)
    except Exception as e:
        return handle_exception(e)

@logger.inject_lambda_context
def update_sucursal_handler(event, context):
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        sucursal_id = event['pathParameters']['id']
        body = json.loads(event.get('body', '{}'))
        db = get_tenant_db(tenant_id)

        update_data = {k: v for k, v in body.items() if k not in ['id', '_id', 'tenant_id', 'createdAt']}
        update_data['updatedAt'] = datetime.utcnow()

        result = db["sucursales"].update_one(
            {"_id": ObjectId(sucursal_id)},
            {"$set": update_data}
        )

        if result.matched_count == 0:
            return create_response(404, "Sucursal no encontrada")

        updated_sucursal = db["sucursales"].find_one({"_id": ObjectId(sucursal_id)})
        updated_sucursal['id'] = str(updated_sucursal.pop('_id'))
        if 'createdAt' in updated_sucursal and isinstance(updated_sucursal['createdAt'], datetime):
            updated_sucursal['createdAt'] = updated_sucursal['createdAt'].isoformat()
        if 'updatedAt' in updated_sucursal and isinstance(updated_sucursal['updatedAt'], datetime):
            updated_sucursal['updatedAt'] = updated_sucursal['updatedAt'].isoformat()

        return create_response(200, "Sucursal actualizada", updated_sucursal)
    except Exception as e:
        return handle_exception(e)

@logger.inject_lambda_context
def delete_sucursal_handler(event, context):
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        sucursal_id = event['pathParameters']['id']
        db = get_tenant_db(tenant_id)

        result = db["sucursales"].delete_one({"_id": ObjectId(sucursal_id)})
        if result.deleted_count == 0:
            return create_response(404, "Sucursal no encontrada")

        return create_response(200, "Sucursal eliminada")
    except Exception as e:
        return handle_exception(e)
