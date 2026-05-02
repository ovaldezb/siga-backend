import json
from bson import ObjectId
from datetime import datetime
from aws_lambda_powertools import Logger
from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.infrastructure.database import get_tenant_db
from src.handlers.admin.folios_manager import get_next_folio_handler

logger = Logger()

@logger.inject_lambda_context
def create_orden_handler(event, context):
    vehiculo_id = None
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})\
        
        tenant_id = claims.get('custom:tenant_id')
        
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        body = json.loads(event.get('body', '{}'))
        db = get_tenant_db(tenant_id)

        # 1. Validar folio
        folio = body.get("folio")
        if not folio:
            return create_response(400, "El folio es requerido")

        # 2. VEHÍCULO: Existente o Nuevo
        vehiculo_id_recibido = body.get("vehiculo_id", "").strip() if body.get("vehiculo_id") else ""
        vehiculo_data = body.get("vehiculo_snapshot", {})
        vehiculo_es_nuevo = False  # Flag para saber si hay que hacer rollback

        if vehiculo_id_recibido:
            # Vehículo ya existe en BD: solo usar el ID
            vehiculo_id = vehiculo_id_recibido
            logger.info(f"Vehículo existente reutilizado: {vehiculo_id}")
        else:
            # Vehículo nuevo: validar datos y crear registro
            if not vehiculo_data:
                return create_response(400, "Los datos del vehículo son requeridos")

            vehiculo_doc = {
                "cliente_id": body.get("cliente_snapshot", {}).get("id"),
                "tenant_id": tenant_id,
                "placas": vehiculo_data.get("placas", ""),
                "marca": vehiculo_data.get("marca", ""),
                "modelo": vehiculo_data.get("modelo", ""),
                "anio": vehiculo_data.get("anio"),
                "vin": vehiculo_data.get("vin", ""),
                "color": vehiculo_data.get("color", ""),
                "createdAt": datetime.utcnow(),
            }

            vehiculo_result = db["vehiculos"].insert_one(vehiculo_doc)
            vehiculo_id = vehiculo_result.inserted_id
            vehiculo_es_nuevo = True
            logger.info(f"Vehículo nuevo creado: {vehiculo_id}")

        # 3. Crear la OS con vehiculo_id
        orden_doc = {
            "folio": folio,
            "tenant_id": tenant_id,
            "estado": body.get("estado", "RECEPCION"),
            "cliente_snapshot": body.get("cliente_snapshot"),
            "vehiculo_id": str(vehiculo_id),
            "puntosArreglar": body.get("puntosArreglar", []),
            "falla_reportada": body.get("falla_reportada", ""),
            "diagnostico": body.get("diagnostico", ""),
            "mecanico_id": body.get("mecanico_id"),
            "kilometraje": body.get("kilometraje", 0),
            "nivel_tanque": body.get("nivel_tanque", 0),
            "testigos_encendidos": body.get("testigos_encendidos", []),
            "proximo_cambio_bujias": body.get("proximo_cambio_bujias", 0),
            "proximo_cambio_aceite": body.get("proximo_cambio_aceite", 0),
            "anticipo": body.get("anticipo", 0),
            "total": body.get("total", 0),
            "createdAt": datetime.utcnow(),
            "updatedAt": datetime.utcnow()
        }

        orden_result = db["ordenes_servicio"].insert_one(orden_doc)
        orden_doc["id"] = str(orden_result.inserted_id)
        del orden_doc["_id"]

        # Serializar fechas
        orden_doc["createdAt"] = orden_doc["createdAt"].isoformat()
        orden_doc["updatedAt"] = orden_doc["updatedAt"].isoformat()

        return create_response(201, "Orden de servicio creada exitosamente", orden_doc)

    except Exception as e:
        # ROLLBACK: Solo si el vehículo fue creado nuevo en esta misma operación
        if vehiculo_es_nuevo and vehiculo_id:
            try:
                db = get_tenant_db(event.get('requestContext', {}).get('authorizer', {}).get('claims', {}).get('custom:tenant_id'))
                db["vehiculos"].delete_one({"_id": vehiculo_id})
                logger.warning(f"ROLLBACK: Vehículo nuevo {vehiculo_id} eliminado por error en creación de OS")
            except Exception as rb_error:
                logger.error(f"Error en rollback del vehículo: {rb_error}")
        return handle_exception(e)

@logger.inject_lambda_context
def list_ordenes_handler(event, context):
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        
        db = get_tenant_db(tenant_id)
        ordenes_cursor = db["ordenes_servicio"].find().sort("createdAt", -1)
        
        ordenes = []
        for o in ordenes_cursor:
            o['id'] = str(o['_id'])
            del o['_id']
            # Serializar fechas
            if 'createdAt' in o and isinstance(o['createdAt'], datetime):
                o['createdAt'] = o['createdAt'].isoformat()
            if 'updatedAt' in o and isinstance(o['updatedAt'], datetime):
                o['updatedAt'] = o['updatedAt'].isoformat()
            ordenes.append(o)
            
        return create_response(200, "Órdenes recuperadas", ordenes)
    except Exception as e:
        return handle_exception(e)
@logger.inject_lambda_context
def update_orden_handler(event, context):
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        orden_id = event['pathParameters']['id']
        body = json.loads(event.get('body', '{}'))
        
        db = get_tenant_db(tenant_id)
        
        update_data = {}
        if 'estado' in body: update_data['estado'] = body['estado']
        if 'motivo_cancelacion' in body: update_data['motivo_cancelacion'] = body['motivo_cancelacion']
        if 'puntosArreglar' in body: update_data['puntosArreglar'] = body['puntosArreglar']
        if 'total' in body: update_data['total'] = body['total']
        
        update_data['updatedAt'] = datetime.now().isoformat()
        
        db["ordenes_servicio"].update_one({"_id": ObjectId(orden_id)}, {"$set": update_data})
        
        # Recuperar actualizada
        orden = db["ordenes_servicio"].find_one({"_id": ObjectId(orden_id)})
        orden['id'] = str(orden['_id'])
        del orden['_id']

        # Serializar fechas para JSON
        if 'createdAt' in orden and isinstance(orden['createdAt'], datetime):
            orden['createdAt'] = orden['createdAt'].isoformat()
        if 'updatedAt' in orden and isinstance(orden['updatedAt'], datetime):
            orden['updatedAt'] = orden['updatedAt'].isoformat()
        
        return create_response(200, "Orden actualizada", orden)
    except Exception as e:
        return handle_exception(e)
