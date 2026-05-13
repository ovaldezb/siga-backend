import json
from datetime import datetime
from aws_lambda_powertools import Logger
from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.infrastructure.database import get_tenant_db
from src.handlers.admin.folios_manager import _get_next_folio_internal
from bson import ObjectId

logger = Logger()

IVA_RATE = 0.16  # Tasa de IVA en México


def create_venta_handler(event, context):
    """POST /ventas — Registra una venta, descuenta inventario y liga OS."""
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No autorizado")

        usuario_id = claims.get('sub', 'unknown')
        usuario_nombre = claims.get('name') or claims.get('email') or 'unknown'

        body = json.loads(event.get('body', '{}'))
        db = get_tenant_db(tenant_id)

        # 1. Preparar el objeto de Venta
        items = body.get('items', [])
        sucursal_id = body.get('sucursal_id') or body.get('sucursalId')
        orden_id = body.get('orden_id')

        if not items:
            return create_response(400, "Se requiere al menos un item para la venta.")

        if not sucursal_id:
            return create_response(400, "Se requiere sucursal_id para registrar la venta.")

        # 2. CALCULAR TOTALES EN SERVIDOR (no confiar en frontend)
        subtotal_calculado = 0.0
        for item in items:
            precio = float(item.get('precio_unitario', 0))
            cantidad = int(item.get('cantidad', 0))
            subtotal_calculado += precio * cantidad

        descuento = float(body.get('descuento', 0))
        subtotal_neto = max(0, subtotal_calculado - descuento)
        iva_calculado = round(subtotal_neto * IVA_RATE, 2)
        total_calculado = round(subtotal_neto + iva_calculado, 2)

        # 3. FOLIO SECUENCIAL (MongoDB atómico, no UUID)
        folio = _get_next_folio_internal(tenant_id, "venta", sucursal_id)

        # 3.5 VALIDAR CRÉDITO SI APLICA
        metodo_pago = body.get('metodo_pago', 'EFECTIVO').upper()
        if metodo_pago == 'CREDITO':
            cliente_id = body.get('cliente_id')
            if not cliente_id or cliente_id == 'PUBLICO_GENERAL':
                return create_response(400, "Ventas a crédito requieren un cliente registrado.")
            
            cliente = db["clientes"].find_one({"_id": ObjectId(cliente_id)})
            if not cliente:
                return create_response(404, "Cliente no encontrado.")
            
            limite = float(cliente.get('limite_credito', 0))
            # TODO: En un sistema completo, restar saldo deudor actual
            if total_calculado > limite:
                 return create_response(400, f"Crédito insuficiente. Límite: ${limite:,.2f}, Compra: ${total_calculado:,.2f}")

        nueva_venta = {
            "folio": folio,
            "cliente_id": body.get('cliente_id', 'PUBLICO_GENERAL'),
            "sucursal_id": sucursal_id,
            "items": items,
            "subtotal": round(subtotal_calculado, 2),
            "descuento": descuento,
            "iva": iva_calculado,
            "total": total_calculado,
            "metodo_pago": metodo_pago,
            "pagos": body.get('pagos', []),
            "orden_id": orden_id,
            "usuario_id": usuario_id,
            "usuario_nombre": usuario_nombre,
            "tenant_id": tenant_id,
            "createdAt": datetime.utcnow(),
        }

        # 4. Insertar Venta
        result = db["ventas"].insert_one(nueva_venta)
        nueva_venta["id"] = str(result.inserted_id)

        # 5. PROCESAR LOGICA DE NEGOCIO — Descontar stock
        for item in items:
            producto = item.get('producto', {})
            item_id = producto.get('id')
            cantidad = int(item.get('cantidad', 0))

            # Descontar stock si es un producto con inventario
            if item_id and item_id != 'manual' and producto.get('tipo') == 'PRODUCTO':
                try:
                    # Actualización atómica: solo descuenta si hay stock suficiente
                    update_result = db["items"].update_one(
                        {"_id": ObjectId(item_id), "stock": {"$gte": cantidad}},
                        {"$inc": {"stock": -cantidad}}
                    )
                    if update_result.modified_count > 0:
                        logger.info(f"Stock descontado para {item_id}: -{cantidad}")
                    else:
                        logger.warning(f"Stock insuficiente o item no encontrado: {item_id}")
                except Exception as stock_err:
                    logger.warning(f"Error al descontar stock para {item_id}: {stock_err}")

        # 6. CERRAR ORDEN DE SERVICIO (Integración)
        if orden_id:
            try:
                db["ordenes_servicio"].update_one(
                    {"_id": ObjectId(orden_id)},
                    {"$set": {
                        "estado": "ENTREGADO",
                        "pagada": True,
                        "venta_id": nueva_venta["id"],
                        "venta_folio": folio,
                        "updatedAt": datetime.utcnow()
                    }}
                )
                logger.info(f"Orden de Servicio {orden_id} marcada como PAGADA.")
            except Exception as os_err:
                logger.warning(f"Error al cerrar OS {orden_id}: {os_err}")

        # Limpiar para respuesta JSON
        if '_id' in nueva_venta:
            del nueva_venta['_id']

        return create_response(201, "Venta procesada con éxito", nueva_venta)

    except Exception as e:
        return handle_exception(e)
