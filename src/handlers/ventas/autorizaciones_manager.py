"""Autorizaciones del POS. Handler espejo de go/internal/autorizaciones (rollback)."""
from src.shared.utils.auth_utils import get_claims, get_groups
import json
from bson import ObjectId
from datetime import datetime
from bson.errors import InvalidId
from aws_lambda_powertools import Logger
from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.infrastructure.database import get_tenant_db
from src.shared.utils.date_utils import iso_utc

logger = Logger()

ESTADOS = {"PENDIENTE", "APROBADA", "RECHAZADA"}


def _nombre(claims, por_defecto):
    return (claims.get('name') or '').strip() or (claims.get('email') or '').strip() or por_defecto


def _es_admin(claims):
    return bool({'ADMIN', 'SUPER_ADMIN'} & set(get_groups(claims)))

@logger.inject_lambda_context
def create_autorizacion_handler(event, context):
    try:
        claims =get_claims(event)
        tenant_id = claims.get('custom:tenant_id')
        solicitante_id = claims.get('sub')
        solicitante_nombre = _nombre(claims, 'Usuario Desconocido')

        if not tenant_id:
            return create_response(403, "No tenantId")

        body = json.loads(event.get('body') or '{}')
        sucursal_id = (body.get('sucursal_id') or '').strip()
        tipo = body.get('tipo') or 'PRECIO_POS' # PRECIO_POS, TRASPASO, etc
        metadata = body.get('metadata') if isinstance(body.get('metadata'), dict) else {}
        if not sucursal_id:
            return create_response(400, "El campo 'sucursal_id' es obligatorio.")
        precio = metadata.get('precio_solicitado')
        if tipo == 'PRECIO_POS' and (isinstance(precio, bool) or not isinstance(precio, (int, float)) or precio < 0):
            return create_response(400, "El precio solicitado debe ser un número mayor o igual a cero.")
        
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
        if not tenant_id:
            return create_response(403, "No tenantId")

        query_params = event.get('queryStringParameters') or {}
        # Uno o varios estados separados por coma (el POS pide APROBADA,RECHAZADA).
        estados = [e.strip().upper() for e in (query_params.get('estado') or '').split(',') if e.strip()] or ['PENDIENTE']
        invalidos = [e for e in estados if e not in ESTADOS]
        if invalidos:
            return create_response(400, f"Solicitud inválida: estado '{invalidos[0]}' no válido")
        sucursal_id = query_params.get('sucursal_id')

        filter_query = {"tenant_id": tenant_id, "estado": {"$in": estados}}
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
        aprobador_nombre = _nombre(claims, 'Admin')
        if not tenant_id:
            return create_response(403, "No tenantId")
        # Antes cualquier usuario (el propio cajero) podía aprobarse el precio especial.
        if not _es_admin(claims):
            return create_response(403, "Solo un administrador puede resolver autorizaciones.")

        try:
            auth_oid = ObjectId((event.get('pathParameters') or {}).get('id'))
        except (InvalidId, TypeError):
            return create_response(400, "Solicitud inválida: id de autorización inválido")
        body = json.loads(event.get('body') or '{}')
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
            {"_id": auth_oid, "tenant_id": tenant_id, "estado": "PENDIENTE"},
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