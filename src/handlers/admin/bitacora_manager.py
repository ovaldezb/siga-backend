"""Consulta de la bitácora de auditoría (ver src/shared/utils/bitacora.py).

Sólo administración: es la revisión de lo que el personal hizo sin pedir permiso
(hoy, cobrar mano de obra a un precio capturado en caja). Revisar un evento sólo
le pone la marca de visto; lo registrado no se edita ni se borra.
"""
import json
from datetime import datetime, timedelta
from aws_lambda_powertools import Logger
from bson import ObjectId
from bson.errors import InvalidId
from pymongo import ReturnDocument
from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.utils.auth_utils import get_claims, get_groups
from src.shared.infrastructure.database import get_tenant_db
from src.shared.utils.date_utils import iso_utc

logger = Logger()


def _es_admin(claims):
    return bool({'ADMIN', 'SUPER_ADMIN'} & set(get_groups(claims)))


def _serializar(doc):
    doc['id'] = str(doc.pop('_id'))
    for campo in ('ts', 'revisada_at'):
        if isinstance(doc.get(campo), datetime):
            doc[campo] = iso_utc(doc[campo])
    return doc


@logger.inject_lambda_context
def list_bitacora_handler(event, context):
    """GET /bitacora?tipo=&revisada=true|false&sucursal_id=&dias=30&limit=50"""
    try:
        claims = get_claims(event)
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No autorizado")
        if not _es_admin(claims):
            return create_response(403, "Sólo administración puede consultar la bitácora.")

        qp = event.get('queryStringParameters') or {}
        filtro = {}
        if qp.get('tipo'):
            filtro['tipo'] = qp['tipo']
        if qp.get('sucursal_id'):
            filtro['sucursal_id'] = qp['sucursal_id']
        revisada = (qp.get('revisada') or '').lower()
        if revisada in ('true', 'false'):
            filtro['revisada'] = revisada == 'true'
        try:
            dias = min(max(int(qp.get('dias') or 30), 1), 366)
            limit = min(max(int(qp.get('limit') or 50), 1), 200)
        except (TypeError, ValueError):
            return create_response(400, "Parámetros 'dias' y 'limit' deben ser numéricos.")
        filtro['ts'] = {'$gte': datetime.utcnow() - timedelta(days=dias)}

        db = get_tenant_db(tenant_id)
        eventos = [_serializar(d) for d in db["bitacora"].find(filtro).sort("ts", -1).limit(limit)]
        pendientes = db["bitacora"].count_documents({**filtro, 'revisada': False}) if 'revisada' not in filtro else None
        return create_response(200, "Bitácora", {"items": eventos, "pendientes": pendientes})
    except Exception as e:
        return handle_exception(e, event)


@logger.inject_lambda_context
def revisar_bitacora_handler(event, context):
    """PUT /bitacora/{id}/revisar — marca el evento como revisado por el admin en turno.
    Body opcional: {"nota": "..."}."""
    try:
        claims = get_claims(event)
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No autorizado")
        if not _es_admin(claims):
            return create_response(403, "Sólo administración puede revisar la bitácora.")

        try:
            oid = ObjectId((event.get('pathParameters') or {}).get('id'))
        except (InvalidId, TypeError):
            return create_response(400, "id inválido.")
        body = json.loads(event.get('body') or '{}')
        nota = (body.get('nota') or '').strip()[:500] or None

        db = get_tenant_db(tenant_id)
        nombre = (claims.get('name') or '').strip() or (claims.get('email') or '').strip() or 'Administrador'
        # Sólo la primera revisión cuenta: no se reescribe quién la vio primero.
        doc = db["bitacora"].find_one_and_update(
            {"_id": oid, "revisada": False},
            {"$set": {
                "revisada": True,
                "revisada_por": {"id": claims.get('sub'), "nombre": nombre},
                "revisada_at": datetime.utcnow(),
                "nota_revision": nota,
            }},
            return_document=ReturnDocument.AFTER,
        )
        if not doc:
            if db["bitacora"].count_documents({"_id": oid}, limit=1):
                return create_response(409, "El evento ya estaba revisado.")
            return create_response(404, "Evento no encontrado.")
        return create_response(200, "Evento revisado", _serializar(doc))
    except Exception as e:
        return handle_exception(e, event)
