import json
from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.infrastructure.database import get_tenant_db

from pymongo import ReturnDocument

def _get_next_folio_internal(tenant_id, tipo, sucursal_id):
    """Lógica interna para obtener y formatear el siguiente folio."""
    db = get_tenant_db(tenant_id)
    
    query = {"tipo": tipo, "sucursal_id": sucursal_id}
    
    # Operación atómica find_one_and_update
    result = db.folios.find_one_and_update(
        query,
        {"$inc": {"secuencia": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER
    )

    secuencia = result.get('secuencia', 1)
    
    # Formateo (ej: V-20240513-0001)
    from datetime import datetime
    date_str = datetime.now().strftime('%Y%m%d')
    prefix = "V" if tipo == "venta" else tipo.upper()
    
    return f"{prefix}-{date_str}-{str(secuencia).zfill(4)}"

def get_next_folio_handler(event, context):
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        tipo = event.get('pathParameters', {}).get('tipo', 'os')
        
        query_params = event.get('queryStringParameters') or {}
        sucursal_id = query_params.get('sucursalId')

        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")
            
        if not sucursal_id:
            return create_response(400, "Se requiere el sucursalId para generar el folio.")

        folio_formateado = _get_next_folio_internal(tenant_id, tipo, sucursal_id)

        return create_response(200, "Siguiente folio obtenido", {
            "folio": folio_formateado,
            "tipo": tipo,
            "sucursalId": sucursal_id
        })

    except Exception as e:
        return handle_exception(e)
