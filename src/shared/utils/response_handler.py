import json
from typing import Any, Dict, Optional
from aws_lambda_powertools import Logger

logger = Logger()

def create_response(status_code: int, message: str, data: Optional[Any] = None) -> Dict[str, Any]:
    """
    Standardized response format for SIGA Backend.
    """
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
            "Access-Control-Allow-Credentials": True,
        },
        "body": json.dumps(body)
    }

def handle_exception(e: Exception) -> Dict[str, Any]:
    """
    Standardized exception handler (handlerAdvice).
    """
    logger.exception(f"An error occurred: {str(e)}")
    
    # Generic error message for the user as requested
    return create_response(500, "Ha ocurrido un error interno en el servidor.")
