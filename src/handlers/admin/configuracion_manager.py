import json
from datetime import datetime
from aws_lambda_powertools import Logger
from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.infrastructure.database import get_tenant_db
from src.shared.utils.auth_utils import parse_object_id, is_admin
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
                    {"id": "bosch",     "nombre": "Bosch",     "activa": True},
                    {"id": "brembo",    "nombre": "Brembo",    "activa": True},
                    {"id": "castrol",   "nombre": "Castrol",   "activa": True},
                    {"id": "acdelco",   "nombre": "ACDelco",   "activa": True},
                    {"id": "michelin",  "nombre": "Michelin",  "activa": True},
                    {"id": "ngk",       "nombre": "NGK",       "activa": True},
                    {"id": "gonher",    "nombre": "Gonher",    "activa": True},
                    {"id": "lth",       "nombre": "LTH",       "activa": True},
                    {"id": "generica",  "nombre": "Genérica",  "activa": True}
                ],
                "gastos_fijos_catalogo": [
                    {"id": "luz",       "nombre": "Luz",       "categoria": "Servicios", "monto_estimado": 0, "activo": True, "icono": "ri-lightbulb-flash-line"},
                    {"id": "agua",      "nombre": "Agua",      "categoria": "Servicios", "monto_estimado": 0, "activo": True, "icono": "ri-drop-line"},
                    {"id": "internet",  "nombre": "Internet",  "categoria": "Servicios", "monto_estimado": 0, "activo": True, "icono": "ri-wifi-line"},
                    {"id": "renta",     "nombre": "Renta",     "categoria": "Inmueble",  "monto_estimado": 0, "activo": True, "icono": "ri-store-2-line"},
                    {"id": "sueldos",   "nombre": "Sueldos",   "categoria": "Nómina",   "monto_estimado": 0, "activo": True, "icono": "ri-team-line"},
                ],
                # Templates sugeridos de puntos de revisión. Cada template es un
                # conjunto nombrado de puntos que el módulo de Órdenes de Servicio
                # puede inyectar al crear/editar una orden.
                "templates_revision": [],
                "tasas": {
                    "iva": 0.16
                },
                "permisos_modulos": {
                    "Dashboard": ["SUPER_ADMIN", "ADMIN", "ASESOR", "MECANICO", "CAJERO"],
                    "Taller": ["SUPER_ADMIN"],
                    "Clientes": ["ADMIN", "ASESOR"],
                    "Vehículos": ["ADMIN", "ASESOR"],
                    "Inventario": ["ADMIN", "ASESOR"],
                    "Órdenes de Servicio": ["ADMIN", "ASESOR", "MECANICO"],
                    "Punto de Venta": ["ADMIN", "ASESOR", "CAJERO"],
                    "Citas": ["ADMIN", "ASESOR"],
                    "Contabilidad": ["ADMIN", "ASESOR"],
                    "Proveedores": ["ADMIN", "ASESOR"],
                    "Técnicos": ["ADMIN", "ASESOR"],
                    "Sucursales": ["ADMIN"],
                    "Reportes": ["ADMIN", "ASESOR"],
                    "Usuarios": ["ADMIN"],
                    "Configuración": ["ADMIN"]
                }
            }
            db["configuracion"].insert_one(config)

        # Migración suave: tenants viejos sin gastos_fijos_catalogo lo reciben vacío
        # (no sembramos defaults para no contaminar configs ya en uso).
        if 'gastos_fijos_catalogo' not in config:
            config['gastos_fijos_catalogo'] = []
            db["configuracion"].update_one(
                {"tenant_id": tenant_id},
                {"$set": {"gastos_fijos_catalogo": []}}
            )

        # Migración suave: tenants viejos sin templates_revision lo reciben vacío.
        if 'templates_revision' not in config:
            config['templates_revision'] = []
            db["configuracion"].update_one(
                {"tenant_id": tenant_id},
                {"$set": {"templates_revision": []}}
            )

        if '_id' in config:
            config['id'] = str(config.pop('_id'))
            
        return create_response(200, "Configuración obtenida", config)
    except Exception as e:
        return handle_exception(e)

def update_config_handler(event, context):
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        
        if not is_admin(claims):
            return create_response(403, "No tiene permisos para modificar la configuración.")
            
        body = json.loads(event.get('body', '{}'))
        db = get_tenant_db(tenant_id)
        
        body['updatedAt'] = datetime.utcnow()
        if 'id' in body:
            del body['id']
            
        db["configuracion"].update_one(
            {"tenant_id": tenant_id},
            {"$set": body},
            upsert=True
        )
        
        return create_response(200, "Configuración actualizada")
    except Exception as e:
        return handle_exception(e)
