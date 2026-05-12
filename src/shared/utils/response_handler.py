import json
from typing import Any, Dict, Optional
from aws_lambda_powertools import Logger
from bson.errors import InvalidId

from datetime import datetime
from bson import ObjectId

logger = Logger()

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

    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS,PATCH",
            "Access-Control-Allow-Headers": "Content-Type,X-Amz-Date,Authorization,X-Api-Key,X-Amz-Security-Token",
            "Access-Control-Allow-Credentials": "true",
        },
        "body": json.dumps(body, cls=MongoJSONEncoder)
    }


def handle_exception(e: Exception) -> Dict[str, Any]:
    """Map common client-side exceptions to 4xx; otherwise 500 with generic message."""
    if isinstance(e, (KeyError, ValueError, InvalidId)):
        logger.warning(f"Client error: {type(e).__name__}: {str(e)}")
        return create_response(400, f"Solicitud inválida: {str(e)}")

    logger.exception(f"An error occurred: {str(e)}")
    return create_response(500, "Ha ocurrido un error interno en el servidor.")
