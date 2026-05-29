from src.shared.utils.auth_utils import get_claims
import json
from bson import ObjectId
from datetime import datetime
from aws_lambda_powertools import Logger
from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.infrastructure.database import get_tenant_db
from src.shared.utils.date_utils import iso_utc

logger = Logger()

@logger.inject_lambda_context
def create_autorizacion_handler(event, context):
    try:
        claims =get_claims(event)
        tenant_id = claims.get('custom:tenant_id')
        solicitante_id = claims.get('sub')
        solicitante_nombre = claims.get('name', 'Usuario Desconocido')

        if not tenant_id:
            return create_response(403, "No tenantId")

        body = json.loads(event.get('body', '{}'))
        sucursal_id = body.get('sucursal_id')
        tipo = body.get('tipo', 'PRECIO_POS') # PRECIO_POS, TRASPASO, etc
        metadata = body.get('metadata', {})
        
        db = get_tenant_db(tenant_id)

        doc = {
            "tenant_id": tenant_id,
            "sucursal_id": sucursal_id,
            "tipo": tipo,
            "estado": "PENDIENTE",
            "solicitante": {"id": solicitante_id, "nombre": solicitante_nombre},
            "metadata": metadata,
            "createdAt": datetime.utcnow()
        }

        result = db["autorizaciones"].insert_one(doc)
        doc["id"] = str(result.inserted_id)
        del doc["_id"]
        doc["createdAt"] = iso_utc(doc["createdAt"])

        return create_response(201, "Autorización solicitada", doc)
    except Exception as e:
        return handle_exception(e)

@logger.inject_lambda_context
def list_autorizaciones_handler(event, context):
    try:
        claims =get_claims(event)
        tenant_id = claims.get('custom:tenant_id')

        query_params = event.get('queryStringParameters') or {}
        estado = query_params.get('estado', 'PENDIENTE')
        sucursal_id = query_params.get('sucursal_id')

        filter_query = {"tenant_id": tenant_id, "estado": estado}
        if sucursal_id:
            filter_query["sucursal_id"] = sucursal_id

        db = get_tenant_db(tenant_id)
        cursor = db["autorizaciones"].find(filter_query).sort("createdAt", -1).limit(50)
        
        items = []
        for doc in cursor:
            doc['id'] = str(doc.pop('_id'))
            if 'createdAt' in doc and isinstance(doc['createdAt'], datetime):
                doc['createdAt'] = iso_utc(doc['createdAt'])
            if 'updatedAt' in doc and isinstance(doc['updatedAt'], datetime):
                doc['updatedAt'] = iso_utc(doc['updatedAt'])
            items.append(doc)

        return create_response(200, "Autorizaciones", {"items": items})
    except Exception as e:
        return handle_exception(e)

@logger.inject_lambda_context
def update_autorizacion_handler(event, context):
    try:
        claims =get_claims(event)
        tenant_id = claims.get('custom:tenant_id')
        aprobador_id = claims.get('sub')
        aprobador_nombre = claims.get('name', 'Admin')

        auth_id = event['pathParameters']['id']
        body = json.loads(event.get('body', '{}'))
        nuevo_estado = body.get('estado') # APROBADA o RECHAZADA

        if nuevo_estado not in ['APROBADA', 'RECHAZADA']:
            return create_response(400, "Estado inválido")

        db = get_tenant_db(tenant_id)

        update_data = {
            "estado": nuevo_estado,
            "aprobador": {"id": aprobador_id, "nombre": aprobador_nombre},
            "updatedAt": datetime.utcnow()
        }

        from pymongo import ReturnDocument
        result = db["autorizaciones"].find_one_and_update(
            {"_id": ObjectId(auth_id), "tenant_id": tenant_id, "estado": "PENDIENTE"},
            {"$set": update_data},
            return_document=ReturnDocument.AFTER
        )

        if not result:
            return create_response(404, "Autorización no encontrada o ya resuelta.")

        result['id'] = str(result.pop('_id'))
        if isinstance(result.get('createdAt'), datetime):
            result['createdAt'] = iso_utc(result['createdAt'])
        if isinstance(result.get('updatedAt'), datetime):
            result['updatedAt'] = iso_utc(result['updatedAt'])

        return create_response(200, "Autorización actualizada", result)
    except Exception as e:
        return handle_exception(e)