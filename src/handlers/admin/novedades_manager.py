"""Novedades de la plataforma: avisos de nuevas funciones y correcciones.

El operador (SUPER_ADMIN) las publica una vez y les llegan a todos los usuarios de
todos los talleres en Notificaciones, o sólo a los talleres de `tenant_ids` cuando
el aviso es para alguien en particular. Viven en `_platform` porque no son de ningún
taller. Lo leído se guarda por usuario (`sub` de Cognito) en
`_platform.novedades_lecturas`, así cada persona descarta las suyas.
"""
import json
from datetime import datetime, timedelta
from aws_lambda_powertools import Logger
from bson import ObjectId
from bson.errors import InvalidId
from pymongo import ReturnDocument
from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.utils.auth_utils import get_claims, is_super_admin
from src.shared.infrastructure.database import get_platform_db
from src.shared.utils.date_utils import iso_utc

logger = Logger()

TIPOS = ("FEATURE", "FIX", "AVISO")
# Lo que un usuario ve en su campana: lo publicado en este periodo.
VENTANA_DIAS = 120
LIMITE = 50


def _serializar(doc, leidas=None):
    doc["id"] = str(doc.pop("_id"))
    for campo in ("createdAt", "updatedAt"):
        if isinstance(doc.get(campo), datetime):
            doc[campo] = iso_utc(doc[campo])
    if leidas is not None:
        doc["leida"] = doc["id"] in leidas
    return doc


def _validar(body, parcial=False) -> tuple[dict, str | None]:
    """Devuelve (campos, error). En edición (parcial) sólo valida lo que venga."""
    campos: dict = {}
    if not parcial or "titulo" in body:
        titulo = " ".join(str(body.get("titulo") or "").split())
        if not titulo:
            return {}, "El título es obligatorio."
        if len(titulo) > 120:
            return {}, "El título admite máximo 120 caracteres."
        campos["titulo"] = titulo
    if not parcial or "descripcion" in body:
        descripcion = str(body.get("descripcion") or "").strip()
        if not descripcion:
            return {}, "La descripción es obligatoria."
        if len(descripcion) > 2000:
            return {}, "La descripción admite máximo 2000 caracteres."
        campos["descripcion"] = descripcion
    if not parcial or "tipo" in body:
        tipo = str(body.get("tipo") or "").upper()
        if tipo not in TIPOS:
            return {}, "Tipo inválido (FEATURE, FIX o AVISO)."
        campos["tipo"] = tipo
    if "version" in body:
        version = str(body.get("version") or "").strip()[:20]
        campos["version"] = version or None
    if "tenant_ids" in body:
        # Vacío o ausente = para todos los talleres.
        tenant_ids = body.get("tenant_ids") or []
        if not isinstance(tenant_ids, list) or len(tenant_ids) > 50:
            return {}, "tenant_ids debe ser una lista de hasta 50 talleres."
        tenant_ids = sorted({str(t).strip() for t in tenant_ids if str(t).strip()})
        if tenant_ids:
            existentes = get_platform_db()["talleres"].count_documents({"tenantId": {"$in": tenant_ids}})
            if existentes != len(tenant_ids):
                return {}, "Alguno de los talleres indicados no existe."
        campos["tenant_ids"] = tenant_ids
    if parcial and "activa" in body:
        campos["activa"] = bool(body.get("activa"))
    return campos, None


@logger.inject_lambda_context
def list_novedades_handler(event, context):
    """GET /novedades — las vigentes con su marca `leida` para el usuario.
    SUPER_ADMIN con ?todas=1 recibe también las ocultas (para administrarlas)."""
    try:
        claims = get_claims(event)
        sub = claims.get("sub")
        if not sub:
            return create_response(403, "No autorizado")

        qp = event.get("queryStringParameters") or {}
        db = get_platform_db()
        if qp.get("todas") == "1" and is_super_admin(claims):
            filtro = {}
        else:
            # Para todos (sin tenant_ids) o dirigida a este taller.
            para_quien = [{"tenant_ids": {"$exists": False}}, {"tenant_ids": None}, {"tenant_ids": {"$size": 0}}]
            if claims.get("custom:tenant_id"):
                para_quien.append({"tenant_ids": claims["custom:tenant_id"]})
            filtro = {"activa": True, "createdAt": {"$gte": datetime.utcnow() - timedelta(days=VENTANA_DIAS)},
                      "$or": para_quien}

        lectura = db["novedades_lecturas"].find_one({"sub": sub}, {"leidas": 1}) or {}
        leidas = set(lectura.get("leidas") or [])
        items = [_serializar(d, leidas) for d in db["novedades"].find(filtro).sort("createdAt", -1).limit(LIMITE)]
        return create_response(200, "Novedades", {
            "items": items,
            "sin_leer": sum(1 for i in items if not i["leida"] and i.get("activa", True)),
        })
    except Exception as e:
        return handle_exception(e, event)


@logger.inject_lambda_context
def create_novedad_handler(event, context):
    """POST /novedades — SUPER_ADMIN publica un aviso para todos los talleres o, con
    `tenant_ids`, sólo para esos."""
    try:
        claims = get_claims(event)
        if not is_super_admin(claims):
            return create_response(403, "Sólo el operador de la plataforma publica novedades.")
        campos, err = _validar(json.loads(event.get("body") or "{}"))
        if err:
            return create_response(400, err)

        ahora = datetime.utcnow()
        doc = {
            **campos,
            "version": campos.get("version"),
            "activa": True,
            "publicado_por": {"id": claims.get("sub"), "nombre": claims.get("name") or claims.get("email")},
            "createdAt": ahora,
            "updatedAt": ahora,
        }
        db = get_platform_db()
        db["novedades"].create_index([("activa", 1), ("createdAt", -1)], name="activa_fecha")
        doc["_id"] = db["novedades"].insert_one(doc).inserted_id
        return create_response(201, "Novedad publicada", _serializar(doc))
    except Exception as e:
        return handle_exception(e, event)


@logger.inject_lambda_context
def update_novedad_handler(event, context):
    """PUT /novedades/{id} — corregir texto u ocultar (activa=false). No se borra:
    quien ya la leyó la tiene en su historial."""
    try:
        claims = get_claims(event)
        if not is_super_admin(claims):
            return create_response(403, "Sólo el operador de la plataforma edita novedades.")
        try:
            oid = ObjectId((event.get("pathParameters") or {}).get("id"))
        except (InvalidId, TypeError):
            return create_response(400, "id inválido.")
        campos, err = _validar(json.loads(event.get("body") or "{}"), parcial=True)
        if err:
            return create_response(400, err)
        if not campos:
            return create_response(400, "No hay campos para actualizar.")
        campos["updatedAt"] = datetime.utcnow()
        doc = get_platform_db()["novedades"].find_one_and_update(
            {"_id": oid}, {"$set": campos}, return_document=ReturnDocument.AFTER)
        if not doc:
            return create_response(404, "Novedad no encontrada.")
        return create_response(200, "Novedad actualizada", _serializar(doc))
    except Exception as e:
        return handle_exception(e, event)


@logger.inject_lambda_context
def marcar_novedades_leidas_handler(event, context):
    """POST /novedades/leidas — body {"ids": [...]}: el usuario las descarta."""
    try:
        claims = get_claims(event)
        sub = claims.get("sub")
        if not sub:
            return create_response(403, "No autorizado")
        ids = json.loads(event.get("body") or "{}").get("ids")
        if not isinstance(ids, list) or not ids or len(ids) > 100:
            return create_response(400, "Envía entre 1 y 100 ids.")
        ids = [str(i) for i in ids if ObjectId.is_valid(str(i))]
        if not ids:
            return create_response(400, "ids inválidos.")
        db = get_platform_db()
        db["novedades_lecturas"].update_one(
            {"sub": sub},
            {"$addToSet": {"leidas": {"$each": ids}},
             "$set": {"tenant_id": claims.get("custom:tenant_id"), "updatedAt": datetime.utcnow()}},
            upsert=True,
        )
        return create_response(200, "Marcadas como leídas", {"ids": ids})
    except Exception as e:
        return handle_exception(e, event)
