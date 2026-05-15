import json
import os
from contextvars import ContextVar
from typing import Any, Dict, List, Optional
from aws_lambda_powertools import Logger
from bson.errors import InvalidId

from datetime import datetime
from bson import ObjectId

logger = Logger()

# Origin de la request actual (se setea por handler con `set_request_origin(event)`
# si quiere permitir cookies/credentials cross-origin).
_request_origin: ContextVar[str] = ContextVar("_request_origin", default="")


def set_request_origin(event: Dict[str, Any]) -> None:
    """Registra el Origin de la request actual para que create_response pueda decidir CORS.

    Llamar al inicio de cada handler si se planea usar Allow-Credentials (cookies/withCredentials).
    Si no se llama, la respuesta cae al comodín sin credentials, que es spec-compliant.
    """
    headers = event.get("headers") or {}
    origin = headers.get("Origin") or headers.get("origin") or ""
    _request_origin.set(origin)


def _allowed_origins() -> List[str]:
    raw = os.environ.get("ALLOWED_ORIGINS", "").strip()
    if not raw:
        return []
    return [o.strip() for o in raw.split(",") if o.strip()]


def _cors_headers() -> Dict[str, str]:
    """Headers CORS spec-compliant.

    Con `ALLOWED_ORIGINS` (env): echo del Origin si está en la lista + Allow-Credentials=true.
    Sin allowlist: Allow-Origin=* y SIN Allow-Credentials (la combinación * + credentials
    es inválida por spec — los browsers la rechazan si alguien activa withCredentials).
    """
    allowed = _allowed_origins()
    if allowed:
        origin = _request_origin.get()
        chosen = origin if origin in allowed else allowed[0]
        return {
            "Access-Control-Allow-Origin": chosen,
            "Access-Control-Allow-Credentials": "true",
            "Vary": "Origin",
        }
    return {
        "Access-Control-Allow-Origin": "*",
    }

class MongoJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, ObjectId):
            return str(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)

def create_response(status_code: int, message: str, data: Optional[Any] = None) -> Dict[str, Any]:
    """Standardized response format for SIGA Backend."""
    body = {
        "success": status_code < 400,
        "message": message,
        "data": data
    }

    headers = {
        "Content-Type": "application/json",
        "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS,PATCH",
        "Access-Control-Allow-Headers": "Content-Type,X-Amz-Date,Authorization,X-Api-Key,X-Amz-Security-Token",
    }
    headers.update(_cors_headers())

    return {
        "statusCode": status_code,
        "headers": headers,
        "body": json.dumps(body, cls=MongoJSONEncoder)
    }


def handle_exception(e: Exception) -> Dict[str, Any]:
    """Map common client-side exceptions to 4xx; otherwise 500 with generic message."""
    if isinstance(e, (KeyError, ValueError, InvalidId)):
        logger.warning(f"Client error: {type(e).__name__}: {str(e)}")
        return create_response(400, f"Solicitud inválida: {str(e)}")

    logger.exception(f"An error occurred: {str(e)}")
    return create_response(500, "Ha ocurrido un error interno en el servidor.")
