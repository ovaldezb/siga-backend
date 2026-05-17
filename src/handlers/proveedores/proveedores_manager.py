import json
import re
from datetime import datetime
from aws_lambda_powertools import Logger
from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.infrastructure.database import get_tenant_db
from bson import ObjectId
from bson.errors import InvalidId
from pymongo import ReturnDocument
from src.shared.utils.date_utils import iso_utc

logger = Logger()

ALLOWED_FIELDS = {
    "nombre", "contacto", "email", "telefono", "rfc",
    "direccion", "ciudad", "categoria", "notas", "activo", "marcas"
}


def _get_tenant_id(event):
    claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
    return claims.get('custom:tenant_id')


def _serialize(doc):
    doc['id'] = str(doc.pop('_id'))
    return doc


def list_proveedores_handler(event, context):
    try:
        tenant_id = _get_tenant_id(event)
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        query_params = event.get('queryStringParameters') or {}
        search_query = (query_params.get('q') or '').strip()
        page = int(query_params.get('page', 1))
        limit = int(query_params.get('limit', 20))
        skip = (page - 1) * limit

        db = get_tenant_db(tenant_id)

        filter_query = {}
        if search_query:
            regex = re.compile(re.escape(search_query), re.IGNORECASE)
            filter_query = {
                "$or": [
                    {"nombre": regex},
                    {"contacto": regex},
                    {"categoria": regex},
                    {"telefono": regex},
                    {"rfc": regex}
                ]
            }

        total = db.proveedores.count_documents(filter_query)
        proveedores = list(
            db.proveedores.find(filter_query)
            .sort("nombre", 1)
            .skip(skip)
            .limit(limit)
        )
        proveedores = [_serialize(p) for p in proveedores]

        return create_response(200, "Proveedores obtenidos", {
            "items": proveedores,
            "total": total,
            "page": page,
            "limit": limit
        })
    except Exception as e:
        return handle_exception(e)


def create_proveedor_handler(event, context):
    try:
        tenant_id = _get_tenant_id(event)
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        body = json.loads(event.get('body', '{}'))

        if not body.get('nombre'):
            return create_response(400, "El nombre del proveedor es requerido.")

        db = get_tenant_db(tenant_id)

        nuevo = {
            "nombre": body['nombre'],
            "contacto": body.get('contacto'),
            "email": body.get('email'),
            "telefono": body.get('telefono'),
            "rfc": body.get('rfc'),
            "direccion": body.get('direccion'),
            "ciudad": body.get('ciudad'),
            "categoria": body.get('categoria'),
            "notas": body.get('notas'),
            "marcas": body.get('marcas', []),
            "activo": body.get('activo', True),
            "createdAt": iso_utc(),
            "tenant_id": tenant_id
        }

        result = db.proveedores.insert_one(nuevo)
        nuevo['id'] = str(result.inserted_id)
        del nuevo['_id']

        return create_response(201, "Proveedor creado exitosamente", nuevo)
    except Exception as e:
        return handle_exception(e)


def get_proveedor_handler(event, context):
    try:
        tenant_id = _get_tenant_id(event)
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        proveedor_id = event['pathParameters']['id']

        try:
            object_id = ObjectId(proveedor_id)
        except InvalidId:
            return create_response(400, "ID de proveedor inválido.")

        db = get_tenant_db(tenant_id)
        proveedor = db.proveedores.find_one({"_id": object_id})

        if not proveedor:
            return create_response(404, "Proveedor no encontrado.")

        return create_response(200, "Detalle del proveedor", _serialize(proveedor))
    except Exception as e:
        return handle_exception(e)


def update_proveedor_handler(event, context):
    try:
        tenant_id = _get_tenant_id(event)
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        proveedor_id = event['pathParameters']['id']

        try:
            object_id = ObjectId(proveedor_id)
        except InvalidId:
            return create_response(400, "ID de proveedor inválido.")

        body = json.loads(event.get('body', '{}'))
        update_doc = {k: body[k] for k in ALLOWED_FIELDS if k in body}

        if not update_doc:
            return create_response(400, "No hay campos válidos para actualizar.")

        update_doc['updatedAt'] = iso_utc()

        db = get_tenant_db(tenant_id)
        result = db.proveedores.find_one_and_update(
            {"_id": object_id},
            {"$set": update_doc},
            return_document=ReturnDocument.AFTER
        )

        if not result:
            return create_response(404, "Proveedor no encontrado.")

        return create_response(200, "Proveedor actualizado", _serialize(result))
    except Exception as e:
        return handle_exception(e)


def delete_proveedor_handler(event, context):
    try:
        tenant_id = _get_tenant_id(event)
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        proveedor_id = event['pathParameters']['id']

        try:
            object_id = ObjectId(proveedor_id)
        except InvalidId:
            return create_response(400, "ID de proveedor inválido.")

        db = get_tenant_db(tenant_id)
        result = db.proveedores.delete_one({"_id": object_id})

        if result.deleted_count == 0:
            return create_response(404, "Proveedor no encontrado.")

        return create_response(200, "Proveedor eliminado")
    except Exception as e:
        return handle_exception(e)
