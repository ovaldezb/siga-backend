"""Buzón de quejas y sugerencias de los talleres hacia la plataforma.

Cualquier usuario de un taller escribe; el operador (SUPER_ADMIN) ve la bandeja de
todos los talleres, cambia el estado y responde. La respuesta le llega al autor como
notificación (`respuesta_leida = False` hasta que la abre). Vive en `_platform`.
"""
import json
from datetime import datetime, timedelta
from aws_lambda_powertools import Logger
from bson import ObjectId
from bson.errors import InvalidId
from pymongo import ReturnDocument
from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.utils.auth_utils import get_claims, get_groups, is_super_admin
from src.shared.infrastructure.database import get_platform_db
from src.shared.utils.date_utils import iso_utc

logger = Logger()

TIPOS = ("QUEJA", "SUGERENCIA", "FALLA")
ESTADOS = ("NUEVO", "EN_REVISION", "RESUELTO", "DESCARTADO")
# Freno anti-spam por usuario: suficiente para uso normal, corta un script o un doble clic.
MAX_POR_DIA = 10


def _serializar(doc):
    doc["id"] = str(doc.pop("_id"))
    for campo in ("createdAt", "updatedAt", "respondido_at"):
        if isinstance(doc.get(campo), datetime):
            doc[campo] = iso_utc(doc[campo])
    return doc


def _oid(event):
    try:
        return ObjectId((event.get("pathParameters") or {}).get("id"))
    except (InvalidId, TypeError):
        return None


@logger.inject_lambda_context
def create_buzon_handler(event, context):
    """POST /buzon — {tipo, asunto, mensaje, modulo?}"""
    try:
        claims = get_claims(event)
        sub = claims.get("sub")
        tenant_id = claims.get("custom:tenant_id")
        if not sub or not tenant_id:
            return create_response(403, "No autorizado")

        body = json.loads(event.get("body") or "{}")
        tipo = str(body.get("tipo") or "").upper()
        asunto = " ".join(str(body.get("asunto") or "").split())
        mensaje = str(body.get("mensaje") or "").strip()
        modulo = " ".join(str(body.get("modulo") or "").split())[:60] or None
        if tipo not in TIPOS:
            return create_response(400, "Tipo inválido (QUEJA, SUGERENCIA o FALLA).")
        if not asunto or len(asunto) > 120:
            return create_response(400, "El asunto es obligatorio (máximo 120 caracteres).")
        if not mensaje or len(mensaje) > 3000:
            return create_response(400, "El mensaje es obligatorio (máximo 3000 caracteres).")

        db = get_platform_db()
        hoy = db["buzon"].count_documents({
            "usuario.id": sub, "createdAt": {"$gte": datetime.utcnow() - timedelta(days=1)}})
        if hoy >= MAX_POR_DIA:
            return create_response(429, "Llegaste al máximo de mensajes por hoy. Intenta mañana.")

        taller = db["talleres"].find_one({"tenantId": tenant_id}, {"nombreComercial": 1}) or {}
        grupos = get_groups(claims)
        ahora = datetime.utcnow()
        doc = {
            "tenant_id": tenant_id,
            "taller_nombre": taller.get("nombreComercial"),
            "usuario": {
                "id": sub,
                "nombre": claims.get("name") or claims.get("email"),
                "email": claims.get("email"),
                "grupo": grupos[0] if grupos else None,
            },
            "tipo": tipo,
            "asunto": asunto,
            "mensaje": mensaje,
            "modulo": modulo,
            "estado": "NUEVO",
            "respuesta": None,
            "respuesta_leida": True,
            "createdAt": ahora,
            "updatedAt": ahora,
        }
        db["buzon"].create_index([("estado", 1), ("createdAt", -1)], name="estado_fecha")
        db["buzon"].create_index([("usuario.id", 1), ("createdAt", -1)], name="usuario_fecha")
        doc["_id"] = db["buzon"].insert_one(doc).inserted_id
        return create_response(201, "Mensaje enviado. ¡Gracias por escribirnos!", _serializar(doc))
    except Exception as e:
        return handle_exception(e, event)


@logger.inject_lambda_context
def list_buzon_handler(event, context):
    """GET /buzon — SUPER_ADMIN: bandeja de todos los talleres (?estado=&tipo=).
    Cualquier otro usuario: sus propios mensajes con su respuesta."""
    try:
        claims = get_claims(event)
        sub = claims.get("sub")
        if not sub:
            return create_response(403, "No autorizado")
        qp = event.get("queryStringParameters") or {}
        db = get_platform_db()

        if is_super_admin(claims):
            filtro = {}
            if qp.get("estado") in ESTADOS:
                filtro["estado"] = qp["estado"]
            if qp.get("tipo") in TIPOS:
                filtro["tipo"] = qp["tipo"]
            if qp.get("tenant_id"):
                filtro["tenant_id"] = qp["tenant_id"]
            items = [_serializar(d) for d in db["buzon"].find(filtro).sort("createdAt", -1).limit(200)]
            return create_response(200, "Buzón", {
                "items": items, "nuevos": db["buzon"].count_documents({"estado": "NUEVO"})})

        items = [_serializar(d) for d in db["buzon"].find({"usuario.id": sub}).sort("createdAt", -1).limit(50)]
        return create_response(200, "Mis mensajes", {
            "items": items, "respuestas_sin_leer": sum(1 for i in items if not i.get("respuesta_leida", True))})
    except Exception as e:
        return handle_exception(e, event)


@logger.inject_lambda_context
def update_buzon_handler(event, context):
    """PUT /buzon/{id}
    - SUPER_ADMIN: {estado?, respuesta?}. Responder notifica al autor.
    - Autor: sólo {"respuesta_leida": true} sobre su propio mensaje."""
    try:
        claims = get_claims(event)
        sub = claims.get("sub")
        oid = _oid(event)
        if not sub:
            return create_response(403, "No autorizado")
        if not oid:
            return create_response(400, "id inválido.")
        body = json.loads(event.get("body") or "{}")
        db = get_platform_db()

        if is_super_admin(claims):
            cambios: dict = {}
            if "estado" in body:
                if body["estado"] not in ESTADOS:
                    return create_response(400, "Estado inválido.")
                cambios["estado"] = body["estado"]
            if "respuesta" in body:
                respuesta = str(body.get("respuesta") or "").strip()
                if not respuesta or len(respuesta) > 3000:
                    return create_response(400, "La respuesta es obligatoria (máximo 3000 caracteres).")
                cambios.update({
                    "respuesta": respuesta,
                    "respuesta_leida": False,
                    "respondido_por": {"id": sub, "nombre": claims.get("name") or claims.get("email")},
                    "respondido_at": datetime.utcnow(),
                })
                cambios.setdefault("estado", "RESUELTO")
            if not cambios:
                return create_response(400, "No hay cambios.")
            filtro = {"_id": oid}
        else:
            if body.get("respuesta_leida") is not True:
                return create_response(403, "Sólo puedes marcar como leída la respuesta a tu mensaje.")
            cambios: dict = {"respuesta_leida": True}
            filtro = {"_id": oid, "usuario.id": sub}

        cambios["updatedAt"] = datetime.utcnow()
        doc = db["buzon"].find_one_and_update(filtro, {"$set": cambios}, return_document=ReturnDocument.AFTER)
        if not doc:
            return create_response(404, "Mensaje no encontrado.")
        return create_response(200, "Mensaje actualizado", _serializar(doc))
    except Exception as e:
        return handle_exception(e, event)
