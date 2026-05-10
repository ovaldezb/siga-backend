import json
import uuid
from datetime import datetime
from src.utils.response import create_response
from src.utils.db import get_tenant_db
from src.utils.logger import logger
from bson import ObjectId

def create_venta_handler(event, context):
    """POST /ventas — Registra una venta, descuenta inventario y liga OS."""
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id: return create_response(403, "No autorizado")

        body = json.loads(event.get('body', '{}'))
        db = get_tenant_db(tenant_id)

        # 1. Preparar el objeto de Venta
        items = body.get('items', [])
        total = body.get('total', 0)
        orden_id = body.get('orden_id') # Campo clave para la integración

        nueva_venta = {
            "folio": f"V-{datetime.now().strftime('%Y%m%d')}-{str(uuid.uuid4())[:4].upper()}",
            "cliente_id": body.get('cliente_id', 'PUBLICO_GENERAL'),
            "sucursal_id": body.get('sucursal_id'),
            "items": items,
            "subtotal": body.get('subtotal', 0),
            "iva": body.get('iva', 0),
            "descuento": body.get('descuento', 0),
            "total": total,
            "metodo_pago": body.get('metodo_pago', 'EFECTIVO'),
            "orden_id": orden_id,
            "tenant_id": tenant_id,
            "createdAt": datetime.utcnow().isoformat() + "Z"
        }

        # 2. Insertar Venta
        db["ventas"].insert_one(nueva_venta)

        # 3. PROCESAR LOGICA DE NEGOCIO
        for item in items:
            producto = item.get('producto', {})
            item_id = producto.get('id')
            cantidad = item.get('cantidad', 0)

            # Descontar stock si es un producto
            if item_id and producto.get('tipo') == 'PRODUCTO':
                db["items"].update_one(
                    {"_id": ObjectId(item_id)},
                    {"$inc": {"stock": -cantidad}}
                )
                logger.info(f"Stock descontado para {item_id}: -{cantidad}")

        # 4. CERRAR ORDEN DE SERVICIO (Integración)
        if orden_id:
            db["ordenes"].update_one(
                {"_id": ObjectId(orden_id)},
                {"$set": {
                    "estado": "ENTREGADO", # O un nuevo estado "PAGADA"
                    "pagada": True,
                    "venta_id": str(nueva_venta["folio"]),
                    "updatedAt": datetime.utcnow().isoformat() + "Z"
                }}
            )
            logger.info(f"Orden de Servicio {orden_id} marcada como PAGADA.")

        # Limpiar para respuesta
        if '_id' in nueva_venta: del nueva_venta['_id']
        
        return create_response(201, "Venta procesada con éxito", nueva_venta)

    except Exception as e:
        logger.exception("Error en create_venta_handler")
        return create_response(500, str(e))
