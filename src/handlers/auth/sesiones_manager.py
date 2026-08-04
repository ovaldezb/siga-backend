"""Control de sesiones concurrentes por usuario.

Cognito no limita en cuántos dispositivos se firma un mismo usuario, así que el
límite se lleva aquí: cada navegador manda un `device_id` estable y el backend
mantiene el registro de sesiones vivas en `_platform.sesiones_usuario`.

Al abrir una sesión nueva por encima del máximo se revoca la MÁS ANTIGUA (por
`ultimo_acceso`): ese dispositivo lo descubre en su siguiente latido y cierra
sesión solo. El token de Cognito del dispositivo revocado sigue siendo
criptográficamente válido hasta que expire — este control desalienta compartir
la cuenta, no sustituye una revocación dura de tokens.
"""
import json
from datetime import datetime, timedelta
from aws_lambda_powertools import Logger

from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.utils.auth_utils import get_claims
from src.shared.infrastructure.database import get_platform_db

logger = Logger()

COLECCION = "sesiones_usuario"

# Sesiones simultáneas permitidas por usuario.
MAX_SESIONES = 2

# Sin latido en esta ventana la sesión deja de contar para el límite: evita que
# un navegador cerrado sin logout bloquee a su dueño para siempre.
VENTANA_INACTIVIDAD_HORAS = 12

# Los documentos se autolimpian con un índice TTL sobre `expira_en`.
RETENCION_DIAS = 7

MOTIVO_LIMITE = "limite_sesiones"

_indices_listos = False


def _coleccion():
    global _indices_listos
    db = get_platform_db()
    col = db[COLECCION]
    if not _indices_listos:
        try:
            col.create_index(
                [("user_sub", 1), ("device_id", 1)], unique=True, name="uniq_user_device"
            )
            col.create_index("expira_en", expireAfterSeconds=0, name="ttl_expira_en")
            _indices_listos = True
        except Exception as idx_err:  # pragma: no cover - no debe tumbar el login
            logger.warning(f"No se pudieron asegurar índices de sesiones: {idx_err}")
    return col


def _identidad(event):
    """(user_sub, email, tenant_id) del token. `sub` es la identidad estable."""
    claims = get_claims(event)
    return (
        claims.get("sub"),
        claims.get("email"),
        claims.get("custom:tenant_id"),
    )


def _device_id_de(event, body=None):
    if body and body.get("device_id"):
        return str(body["device_id"]).strip()
    params = event.get("queryStringParameters") or {}
    return str(params.get("device_id") or "").strip()


def _sesiones_vivas(col, user_sub, ahora, excluir_device=None):
    """Sesiones no revocadas con latido dentro de la ventana, más reciente primero.

    El desempate por `createdAt` importa: dos dispositivos pueden compartir
    `ultimo_acceso` al milisegundo (dos pestañas latiendo a la vez), y sin un
    segundo criterio la sesión que se revoca al superar el límite sería arbitraria.
    """
    filtro = {
        "user_sub": user_sub,
        "revocada_en": None,
        "ultimo_acceso": {"$gte": ahora - timedelta(hours=VENTANA_INACTIVIDAD_HORAS)},
    }
    if excluir_device:
        filtro["device_id"] = {"$ne": excluir_device}
    return list(col.find(filtro).sort([("ultimo_acceso", -1), ("createdAt", -1)]))


def _registrar(col, user_sub, email, tenant_id, device_id, user_agent, ahora):
    """Alta/refresco de la sesión del dispositivo + poda de las sobrantes.

    Devuelve (sesiones_activas, revocadas).
    """
    col.update_one(
        {"user_sub": user_sub, "device_id": device_id},
        {
            "$set": {
                "email": email,
                "tenant_id": tenant_id,
                "user_agent": user_agent,
                "ultimo_acceso": ahora,
                "expira_en": ahora + timedelta(days=RETENCION_DIAS),
                "revocada_en": None,
                "motivo": None,
            },
            "$setOnInsert": {"createdAt": ahora},
        },
        upsert=True,
    )

    # El dispositivo actual siempre se queda; se revocan las más antiguas del resto.
    otras = _sesiones_vivas(col, user_sub, ahora, excluir_device=device_id)
    sobrantes = otras[MAX_SESIONES - 1:] if MAX_SESIONES > 0 else otras

    revocadas = []
    for doc in sobrantes:
        col.update_one(
            {"_id": doc["_id"]},
            {"$set": {"revocada_en": ahora, "motivo": MOTIVO_LIMITE}},
        )
        revocadas.append({
            "device_id": doc.get("device_id"),
            "ultimo_acceso": doc.get("ultimo_acceso"),
            "user_agent": doc.get("user_agent"),
        })

    if revocadas:
        logger.info(
            f"Límite de {MAX_SESIONES} sesiones: se revocaron {len(revocadas)} sesión(es) de {email}"
        )

    activas = min(len(otras) - len(revocadas) + 1, MAX_SESIONES)
    return activas, revocadas


@logger.inject_lambda_context
def registrar_sesion_handler(event, context):
    """POST /auth/sesiones — el front la llama justo después de un login exitoso."""
    try:
        user_sub, email, tenant_id = _identidad(event)
        if not user_sub:
            return create_response(401, "Token sin identidad de usuario.")

        body = json.loads(event.get("body", "{}") or "{}")
        device_id = _device_id_de(event, body)
        if not device_id:
            return create_response(400, "El campo 'device_id' es obligatorio.")

        ahora = datetime.utcnow()
        col = _coleccion()
        activas, revocadas = _registrar(
            col, user_sub, email, tenant_id, device_id,
            (body.get("user_agent") or "")[:300], ahora
        )

        return create_response(200, "Sesión registrada", {
            "device_id": device_id,
            "sesiones_activas": activas,
            "max_sesiones": MAX_SESIONES,
            "sesiones_cerradas": len(revocadas),
        })
    except Exception as e:
        return handle_exception(e, event)


@logger.inject_lambda_context
def estado_sesion_handler(event, context):
    """GET /auth/sesiones?device_id=… — latido: dice si esta sesión sigue vigente.

    Un dispositivo sin registro (sesión abierta antes de que existiera el control)
    se da de alta aquí mismo en vez de expulsarse, para no cerrarle la sesión a
    nadie por el simple hecho del despliegue.
    """
    try:
        user_sub, email, tenant_id = _identidad(event)
        if not user_sub:
            return create_response(401, "Token sin identidad de usuario.")

        device_id = _device_id_de(event)
        if not device_id:
            return create_response(400, "El parámetro 'device_id' es obligatorio.")

        ahora = datetime.utcnow()
        col = _coleccion()
        doc = col.find_one({"user_sub": user_sub, "device_id": device_id})

        if not doc:
            activas, _ = _registrar(col, user_sub, email, tenant_id, device_id, "", ahora)
            return create_response(200, "Sesión registrada", {
                "vigente": True,
                "sesiones_activas": activas,
                "max_sesiones": MAX_SESIONES,
            })

        if doc.get("revocada_en"):
            return create_response(200, "Sesión revocada", {
                "vigente": False,
                "motivo": doc.get("motivo") or MOTIVO_LIMITE,
                "max_sesiones": MAX_SESIONES,
            })

        col.update_one(
            {"_id": doc["_id"]},
            {"$set": {
                "ultimo_acceso": ahora,
                "expira_en": ahora + timedelta(days=RETENCION_DIAS),
            }},
        )

        return create_response(200, "Sesión vigente", {
            "vigente": True,
            "sesiones_activas": len(_sesiones_vivas(col, user_sub, ahora)),
            "max_sesiones": MAX_SESIONES,
        })
    except Exception as e:
        return handle_exception(e, event)


@logger.inject_lambda_context
def cerrar_sesion_handler(event, context):
    """DELETE /auth/sesiones?device_id=… — libera el cupo al cerrar sesión."""
    try:
        user_sub, _, _ = _identidad(event)
        if not user_sub:
            return create_response(401, "Token sin identidad de usuario.")

        device_id = _device_id_de(event)
        if not device_id:
            return create_response(400, "El parámetro 'device_id' es obligatorio.")

        _coleccion().delete_one({"user_sub": user_sub, "device_id": device_id})
        return create_response(200, "Sesión cerrada")
    except Exception as e:
        return handle_exception(e, event)
