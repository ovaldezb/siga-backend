from typing import Optional, Tuple, Any, Dict, List, Set
from bson import ObjectId
from bson.errors import InvalidId


def get_claims(event) -> Dict[str, Any]:
    if not event:
        return {}
    request_context = event.get('requestContext', {})
    authorizer = request_context.get('authorizer', {}) or {}
    if 'jwt' in authorizer:
        return authorizer.get('jwt', {}).get('claims', {}) or {}
    return authorizer.get('claims', {}) or {}


def get_tenant_id(event) -> Optional[str]:
    claims = get_claims(event)
    return claims.get('custom:tenant_id')


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


# Cache per-Lambda-invocation: la misma Lambda warm puede manejar varios eventos
# pero como Lambda no comparte memoria entre invocaciones distintas en general,
# esto basta para amortizar lookups dentro del mismo request.
_user_sucursales_cache: Dict[str, Optional[Set[str]]] = {}


def is_admin(claims: Dict[str, Any]) -> bool:
    """True si el usuario es ADMIN o SUPER_ADMIN según claims de Cognito."""
    grupo = claims.get('cognito:groups') or ''
    if isinstance(grupo, list):
        grupo = ','.join(grupo)
    return 'ADMIN' in grupo or 'SUPER_ADMIN' in grupo


def get_user_allowed_sucursales(claims: Dict[str, Any], tenant_db) -> Optional[Set[str]]:
    """
    Devuelve el conjunto de sucursal_id permitidos para este usuario.

    - ADMIN/SUPER_ADMIN ⇒ None (sin restricción, ven todas las sucursales del tenant).
    - Otros roles ⇒ set con los ids de su array `sucursales` en la colección `usuarios`.
      Si el usuario no existe o no tiene sucursales asignadas, devuelve set vacío
      (lo que en la práctica bloquea cualquier query con scope).
    """
    if is_admin(claims):
        return None

    email = claims.get('email')
    tenant_id = claims.get('custom:tenant_id')
    if not email or not tenant_id:
        return set()

    cache_key = f"{tenant_id}::{email}"
    if cache_key in _user_sucursales_cache:
        return _user_sucursales_cache[cache_key]

    try:
        user_doc = tenant_db["usuarios"].find_one({"email": email}, {"sucursales": 1})
    except Exception:
        user_doc = None

    sucursales_set: Set[str] = set()
    if user_doc:
        for item in user_doc.get('sucursales', []) or []:
            sid = None
            if isinstance(item, dict):
                sid = item.get('sucursal') or item.get('id') or item.get('sucursal_id')
            elif isinstance(item, str):
                sid = item
            if sid:
                sucursales_set.add(str(sid))

    _user_sucursales_cache[cache_key] = sucursales_set
    return sucursales_set


def resolve_sucursal_scope(
    claims: Dict[str, Any],
    tenant_db,
    requested_sucursal_id: Optional[str]
) -> Tuple[Optional[List[str]], Optional[str]]:
    """
    Resuelve qué sucursal_id usar como filtro para esta request, validando contra
    las sucursales permitidas del usuario.

    Retorna (lista_sucursales_a_filtrar, error_msg).
    - lista_sucursales_a_filtrar:
        · `None` ⇒ admin sin restricción (no filtrar)
        · `[sid]` ⇒ filtrar por esa sucursal específica
        · `[s1, s2, ...]` ⇒ filtrar con $in (no-admin sin sucursal_id explícita)
    - error_msg:
        · `None` ⇒ OK
        · str ⇒ violación de scope (403)
    """
    allowed = get_user_allowed_sucursales(claims, tenant_db)

    # ADMIN: lo que pida, sin restricción
    if allowed is None:
        return ([requested_sucursal_id] if requested_sucursal_id else None), None

    # No-admin sin sucursales asignadas: bloqueado
    if not allowed:
        return None, "Usuario sin sucursales asignadas. Contacte al administrador."

    if requested_sucursal_id:
        if requested_sucursal_id not in allowed:
            return None, "No tiene acceso a esta sucursal."
        return [requested_sucursal_id], None

    # No-admin sin sucursal_id explícita: forzamos filtro al universo de sus sucursales
    return list(allowed), None
