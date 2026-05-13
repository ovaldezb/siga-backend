import json
from datetime import datetime
from aws_lambda_powertools import Logger
from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.infrastructure.database import get_tenant_db
from bson import ObjectId

logger = Logger()

def get_config_handler(event, context):
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        
        db = get_tenant_db(tenant_id)
        config = db["configuracion"].find_one({"tenant_id": tenant_id})
        
        if not config:
            # Configuración por defecto
            config = {
                "tenant_id": tenant_id,
                "metodos_pago": [
                    {"id": "efectivo", "nombre": "Efectivo", "icono": "ri-money-dollar-circle-line", "activo": True, "requiere_referencia": False},
                    {"id": "tarjeta", "nombre": "Tarjeta", "icono": "ri-bank-card-line", "activo": True, "requiere_referencia": True},
                    {"id": "transferencia", "nombre": "Transferencia", "icono": "ri-exchange-line", "activo": True, "requiere_referencia": True},
                    {"id": "credito", "nombre": "Crédito", "icono": "ri-hand-coin-line", "activo": True, "requiere_referencia": False}
                ],
                "marcas": [
                    {"nombre": "General", "activa": True}
                ],
                "tasas": {
                    "iva": 0.16
                }
            }
            db["configuracion"].insert_one(config)

        if '_id' in config:
            config['id'] = str(config.pop('_id'))
            
        return create_response(200, "Configuración obtenida", config)
    except Exception as e:
        return handle_exception(e)

def update_config_handler(event, context):
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        
        body = json.loads(event.get('body', '{}'))
        db = get_tenant_db(tenant_id)
        
        body['updatedAt'] = datetime.utcnow()
        if 'id' in body:
            del body['id']
            
        result = db["configuracion"].update_one(
            {"tenant_id": tenant_id},
            {"$set": body},
            upsert=True
        )
        
        return create_response(200, "Configuración actualizada")
    except Exception as e:
        return handle_exception(e)
