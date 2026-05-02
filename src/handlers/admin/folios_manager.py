import json
from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.infrastructure.database import get_tenant_db

from pymongo import ReturnDocument

def _get_next_folio_internal(tenant_id, tipo):
    """Lógica interna para obtener y formatear el siguiente folio."""
    db = get_tenant_db(tenant_id)
    
    # Operación atómica find_one_and_update
    result = db.folios.find_one_and_update(
        {"tipo": tipo},
        {"$inc": {"secuencia": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER # Devolvemos el valor DESPUÉS del incremento
    )

    secuencia = result.get('secuencia', 1)
    
    # Formateo (ej: OS-0001)
    prefix = tipo.upper()
    return f"{prefix}-{str(secuencia).zfill(4)}"

def get_next_folio_handler(event, context):
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        tipo = event.get('pathParameters', {}).get('tipo', 'os')

        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        folio_formateado = _get_next_folio_internal(tenant_id, tipo)

        return create_response(200, "Siguiente folio obtenido", {
            "folio": folio_formateado,
            "tipo": tipo
        })

    except Exception as e:
        return handle_exception(e)
