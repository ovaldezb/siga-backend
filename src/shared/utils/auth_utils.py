from typing import Optional, Tuple, Any, Dict
from bson import ObjectId
from bson.errors import InvalidId


def get_tenant_id(event) -> Optional[str]:
    claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
    return claims.get('custom:tenant_id')


def get_claims(event) -> Dict[str, Any]:
    return event.get('requestContext', {}).get('authorizer', {}).get('claims', {}) or {}


def parse_object_id(value: str) -> Tuple[Optional[ObjectId], Optional[str]]:
    try:
        return ObjectId(value), None
    except (InvalidId, TypeError):
        return None, "ID inválido."

def try_parse_id(value: Any) -> Any:
    """Retorna siempre el string para evitar problemas de tipo con ObjectId."""
    if value is None:
        return None
    return str(value)
