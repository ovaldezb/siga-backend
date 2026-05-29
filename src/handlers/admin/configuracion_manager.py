import json
from datetime import datetime
from aws_lambda_powertools import Logger
from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.infrastructure.database import get_tenant_db
from src.shared.utils.auth_utils import parse_object_id, is_admin, get_claims
from bson import ObjectId

logger = Logger()

def get_config_handler(event, context):
    try:
        claims =get_claims(event)
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
        claims =get_claims(event)
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
        
        # Sincronizar templates a la colección cotizaciones si viene en el payload
        if "templates_revision" in body:
            try:
                from src.handlers.cotizaciones.cotizaciones_manager import _next_folio_cotizacion, _ensure_folio_index
                templates_revision = body["templates_revision"] or []
                
                # 1. Obtener plantillas existentes en la colección cotizaciones
                cursor = db["cotizaciones"].find({"tenant_id": tenant_id, "tipo": "PLANTILLA"})
                existing_templates = {}
                for doc in cursor:
                    nombre = doc.get("nombre")
                    if nombre:
                        existing_templates[nombre.strip().lower()] = doc
                
                sent_names = set()
                now = datetime.utcnow()
                _ensure_folio_index(db)
                
                for tpl in templates_revision:
                    nombre = (tpl.get("nombre") or "").strip()
                    if not nombre:
                        continue
                    nombre_lower = nombre.lower()
                    sent_names.add(nombre_lower)
                    
                    # Normalizar puntos/items
                    puntos_revision = tpl.get("puntos") or []
                    puntos_arreglar = []
                    for p in puntos_revision:
                        p_nombre = (p.get("nombre") or "").strip()
                        if not p_nombre:
                            continue
                        items_arreglar = []
                        for it in p.get("items") or []:
                            it_nombre = (it.get("nombre") or "").strip()
                            if not it_nombre:
                                continue
                            try:
                                piezas = float(it.get("piezas") or 1)
                            except (TypeError, ValueError):
                                piezas = 1.0
                            try:
                                precio = float(it.get("precioVenta") or 0)
                            except (TypeError, ValueError):
                                precio = 0.0
                            items_arreglar.append({
                                "nombre": it_nombre,
                                "piezas": piezas,
                                "precioVenta": precio,
                                "precioCompra": float(it.get("precioCompra") or 0),
                                "subtotal": piezas * precio,
                                "aprobado": False,
                                "entregado": False,
                                "tipo": it.get("tipo") or "PRODUCTO"
                            })
                        puntos_arreglar.append({
                            "nombre": p_nombre,
                            "items": items_arreglar
                        })
                    
                    # Calcular totales
                    total = 0.0
                    for p in puntos_arreglar:
                        for it in p["items"]:
                            total += it["subtotal"]
                    total = round(total, 2)
                    
                    if nombre_lower in existing_templates:
                        existing_doc = existing_templates[nombre_lower]
                        db["cotizaciones"].update_one(
                            {"_id": existing_doc["_id"]},
                            {"$set": {
                                "puntosArreglar": puntos_arreglar,
                                "subtotal": total,
                                "total": total,
                                "updatedAt": now
                            }}
                        )
                    else:
                        # Crear nueva
                        folio = _next_folio_cotizacion(db, "PLANTILLA", None)
                        new_doc = {
                            "tenant_id": tenant_id,
                            "tipo": "PLANTILLA",
                            "sucursal_id": None,
                            "folio": folio,
                            "nombre": nombre,
                            "marca": tpl.get("marca") or None,
                            "modelo": tpl.get("modelo") or None,
                            "anio_desde": tpl.get("anio_desde"),
                            "anio_hasta": tpl.get("anio_hasta"),
                            "tipo_servicio": tpl.get("tipo_servicio") or None,
                            "cliente_snapshot": {},
                            "vehiculo_snapshot": {},
                            "puntosArreglar": puntos_arreglar,
                            "subtotal": total,
                            "iva": 0.0,
                            "total": total,
                            "observaciones": "",
                            "kilometraje": None,
                            "createdAt": now,
                            "updatedAt": now,
                            "created_by": "configuracion_sync",
                            "legacy_id": tpl.get("id")
                        }
                        db["cotizaciones"].insert_one(new_doc)
                
                # 2. Eliminar plantillas que estaban en cotizaciones pero fueron borradas en la Configuración
                for existing_name_lower, doc in existing_templates.items():
                    if existing_name_lower not in sent_names:
                        db["cotizaciones"].delete_one({"_id": doc["_id"]})
                        
            except Exception as sync_err:
                logger.error(f"Error sincronizando templates a cotizaciones: {sync_err}")
        
        return create_response(200, "Configuración actualizada")
    except Exception as e:
        return handle_exception(e)
