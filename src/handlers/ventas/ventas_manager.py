import json
from datetime import datetime
from aws_lambda_powertools import Logger
from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.utils.auth_utils import try_parse_id
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

        # 2. CALCULAR TOTALES EN SERVIDOR — respetar flag precio_incluye_iva e iva_exento por línea
        #    Convención SIGA: si no viene el flag, asumimos precio_incluye_iva=True (precios capturados YA con IVA).
        base_acum = 0.0   # subtotal (base imponible)
        iva_acum = 0.0
        gross_acum = 0.0  # lo que ve el usuario en el ticket antes de descuento
        for item in items:
            try:
                precio = float(item.get('precio_unitario', 0))
                cantidad = int(item.get('cantidad', 0))
            except (TypeError, ValueError):
                continue
            if precio < 0 or cantidad <= 0:
                continue
            producto = item.get('producto') or {}
            incluye_iva = item.get('precio_incluye_iva',
                                   producto.get('precio_incluye_iva', True))
            iva_exento = item.get('iva_exento',
                                  producto.get('iva_exento', False))
            line_amount = precio * cantidad
            if iva_exento:
                line_base = line_amount
                line_iva = 0.0
                line_gross = line_amount
            elif bool(incluye_iva):
                line_base = line_amount / (1 + IVA_RATE)
                line_iva = line_amount - line_base
                line_gross = line_amount
            else:
                line_base = line_amount
                line_iva = line_amount * IVA_RATE
                line_gross = line_amount + line_iva
            base_acum += line_base
            iva_acum += line_iva
            gross_acum += line_gross

        try:
            descuento = float(body.get('descuento', 0))
        except (TypeError, ValueError):
            descuento = 0.0
        if descuento < 0:
            descuento = 0.0

        # Aplicar descuento al total bruto; rebajar base e IVA proporcionalmente
        total_bruto = max(0.0, gross_acum - descuento)
        if gross_acum > 0:
            factor = total_bruto / gross_acum
            subtotal_calculado = round(base_acum * factor, 2)
            iva_calculado = round(iva_acum * factor, 2)
        else:
            subtotal_calculado = 0.0
            iva_calculado = 0.0
        total_calculado = round(subtotal_calculado + iva_calculado, 2)

        # 3. FOLIO SECUENCIAL (MongoDB atómico, no UUID)
        folio = _get_next_folio_internal(tenant_id, "venta", sucursal_id)

        # 3.5 VALIDAR STOCK ATÓMICAMENTE ANTES DE PROCESAR + SNAPSHOT COSTO POR LÍNEA
        # Reglas:
        #  - Items externos (vienen de proveedor, no son inventario propio) NO se validan
        #    contra `items`, no descuentan stock y su costo_unitario_snapshot proviene de
        #    `costo_proveedor` (capturado por el asesor en la OS).
        #  - Items "manual" antiguos siguen tolerándose (no validan stock).
        #  - Items normales validan stock y toman snapshot del costo promedio del catálogo.
        for item in items:
            producto = item.get('producto', {})
            item_id = producto.get('id')
            es_externo = bool(item.get('es_externo') or producto.get('es_externo'))
            try:
                cantidad = int(item.get('cantidad', 0))
            except (TypeError, ValueError):
                cantidad = 0

            if es_externo:
                # Snapshot del costo pagado al proveedor (cae al margen como COGS de esta OS).
                costo_ext = (
                    item.get('costo_proveedor')
                    or producto.get('costo_proveedor')
                    or producto.get('precio_compra')
                    or 0
                )
                try:
                    item['costo_unitario_snapshot'] = float(costo_ext or 0)
                except (TypeError, ValueError):
                    item['costo_unitario_snapshot'] = 0.0
                # Propagar metadata del proveedor a la línea para reportes contables por OS.
                if not item.get('proveedor_id'):
                    item['proveedor_id'] = producto.get('proveedor_id')
                if not item.get('proveedor_nombre'):
                    item['proveedor_nombre'] = producto.get('proveedor_nombre')
                item['es_externo'] = True
                continue

            if item_id and item_id != 'manual' and producto.get('tipo') == 'PRODUCTO':
                 p = db["items"].find_one({"_id": ObjectId(item_id)})
                 if not p:
                     return create_response(404, f"Producto no encontrado: {producto.get('nombre')}")
                 if p.get('stock', 0) < cantidad:
                      return create_response(400, f"Stock insuficiente para {producto.get('nombre')}. Disponible: {p.get('stock', 0)}")
                 # Snapshot del costo promedio actual: clave para reportes de margen.
                 # Se almacena en la línea misma para que el reporte sea reproducible
                 # aunque el costo del item cambie después.
                 item['costo_unitario_snapshot'] = float(p.get('costo_promedio', p.get('precio_compra', 0)) or 0)

        # 3.6 VALIDAR CRÉDITO SI APLICA (acepta crédito como método único o dentro de pagos[])
        metodo_pago = body.get('metodo_pago', 'EFECTIVO').upper()
        pagos_body = body.get('pagos', []) or []
        monto_credito = 0.0
        for p in pagos_body:
            try:
                if str(p.get('metodo', '')).upper() == 'CREDITO':
                    monto_credito += float(p.get('monto', 0))
            except (ValueError, TypeError):
                pass
        if metodo_pago == 'CREDITO' and monto_credito == 0:
            # Método único = crédito ⇒ todo el total es crédito
            monto_credito = total_calculado

        if monto_credito > 0:
            cliente_id = body.get('cliente_id')
            if not cliente_id or cliente_id == 'PUBLICO_GENERAL':
                return create_response(400, "Ventas a crédito requieren un cliente registrado.")

            cliente = db["clientes"].find_one({"_id": ObjectId(cliente_id)})
            if not cliente:
                return create_response(404, "Cliente no encontrado.")

            limite = float(cliente.get('limite_credito', 0))
            # Saldo deudor previo: ventas con saldo_pendiente > 0 del mismo cliente
            saldo_previo_agg = list(db["ventas"].aggregate([
                {"$match": {"cliente_id": cliente_id, "saldo_pendiente": {"$gt": 0}}},
                {"$group": {"_id": None, "saldo": {"$sum": "$saldo_pendiente"}}}
            ]))
            saldo_previo = float(saldo_previo_agg[0]['saldo']) if saldo_previo_agg else 0.0
            credito_disponible = limite - saldo_previo
            if monto_credito > credito_disponible:
                return create_response(400,
                    f"Crédito insuficiente. Disponible: ${credito_disponible:,.2f} (límite ${limite:,.2f} - saldo ${saldo_previo:,.2f}), Solicitado a crédito: ${monto_credito:,.2f}")

        # 3.7 VINCULAR VEHÍCULO SI VIENE DE OS
        vehiculo_id = body.get('vehiculo_id')
        vehiculo_snapshot = body.get('vehiculo_snapshot')
        if orden_id and not vehiculo_id:
             os_doc = db["ordenes_servicio"].find_one({"_id": ObjectId(orden_id)})
             if os_doc:
                 vehiculo_id = os_doc.get('vehiculo_id')
                 # Si no hay snapshot en body, intentar obtenerlo del vehículo real
                 if not vehiculo_snapshot and vehiculo_id:
                      v_doc = db["vehiculos"].find_one({"_id": ObjectId(vehiculo_id)})
                      if v_doc:
                          v_doc['id'] = str(v_doc.pop('_id'))
                          vehiculo_snapshot = v_doc

        # 3.8 IDEMPOTENCIA: no permitir dos ventas para la misma OS
        if orden_id:
            existente = db["ventas"].find_one({"orden_id": orden_id})
            if existente:
                return create_response(409,
                    f"La orden ya tiene una venta registrada (folio {existente.get('folio')}).",
                    {"venta_id": str(existente.get('_id')), "folio": existente.get('folio')})

        nueva_venta = {
            "folio": folio,
            "cliente_id": body.get('cliente_id', 'PUBLICO_GENERAL'),
            "cliente_nombre": body.get('cliente_nombre', 'Público General'),
            "vehiculo_id": vehiculo_id,
            "vehiculo_snapshot": vehiculo_snapshot,
            "sucursal_id": sucursal_id,
            "items": items,
            "subtotal": round(subtotal_calculado, 2),
            "descuento": descuento,
            "iva": iva_calculado,
            "total": total_calculado,
            "metodo_pago": metodo_pago,
            "pagos": pagos_body,
            "monto_credito": round(monto_credito, 2),
            "saldo_pendiente": round(monto_credito, 2),  # AR: arranca igual al crédito otorgado
            "orden_id": orden_id,
            "usuario_id": usuario_id,
            "usuario_nombre": usuario_nombre,
            "tenant_id": tenant_id,
            "createdAt": datetime.utcnow(),
        }

        # 4. Insertar Venta
        result = db["ventas"].insert_one(nueva_venta)
        nueva_venta["id"] = str(result.inserted_id)

        # 5. PROCESAR LOGICA DE NEGOCIO — Descontar stock (filtrado por sucursal)
        items_sin_stock = []
        for item in items:
            producto = item.get('producto', {})
            item_id = producto.get('id')
            cantidad = int(item.get('cantidad', 0))

            # Piezas externas no viven en inventario: no se descuenta nada.
            if item.get('es_externo') or producto.get('es_externo'):
                continue

            # Descontar stock si es un producto con inventario
            if item_id and item_id != 'manual' and producto.get('tipo') == 'PRODUCTO':
                try:
                    # Atómica + scoped por sucursal para evitar tocar stock de otra sucursal
                    update_result = db["items"].update_one(
                        {"_id": ObjectId(item_id), "sucursal_id": sucursal_id, "stock": {"$gte": cantidad}},
                        {"$inc": {"stock": -cantidad}}
                    )
                    if update_result.modified_count > 0:
                        logger.info(f"Stock descontado para {item_id} en sucursal {sucursal_id}: -{cantidad}")
                    else:
                        logger.warning(f"Stock insuficiente o item/sucursal no coincide: {item_id} / {sucursal_id}")
                        items_sin_stock.append(producto.get('nombre') or item_id)
                except Exception as stock_err:
                    logger.warning(f"Error al descontar stock para {item_id}: {stock_err}")

        # 6. CERRAR ORDEN DE SERVICIO (Integración) — pagada sólo si NO hay crédito pendiente
        if orden_id:
            try:
                os_update = {
                    "estado": "ENTREGADO" if monto_credito == 0 else "FINALIZADO",
                    "pagada": monto_credito == 0,
                    "venta_id": nueva_venta["id"],
                    "venta_folio": folio,
                    "saldo_pendiente": round(monto_credito, 2),
                    "updatedAt": datetime.utcnow()
                }
                db["ordenes_servicio"].update_one(
                    {"_id": ObjectId(orden_id)},
                    {"$set": os_update}
                )
                logger.info(f"OS {orden_id} {'PAGADA' if monto_credito == 0 else f'con saldo ${monto_credito:.2f}'}.")
            except Exception as os_err:
                logger.warning(f"Error al cerrar OS {orden_id}: {os_err}")

        # Adjuntar advertencias al payload para que el front decida si avisar
        if items_sin_stock:
            nueva_venta["warnings"] = [f"Stock no descontado para: {', '.join(items_sin_stock)} (verifique inventario manualmente)"]

        # 7. REGISTRO ATÓMICO EN CAJA: cualquier método "contado" (no crédito) que entre
        #    debe quedar reflejado en caja_sesiones de la sucursal. Antes esto vivía en
        #    el cliente y se perdía si la red cortaba entre POST /ventas y POST /caja/movimientos.
        try:
            sesion = db.caja_sesiones.find_one({"sucursal_id": sucursal_id, "estado": "ABIERTA"})
            if sesion:
                pagos_caja = []
                total_contado = 0.0
                for p in pagos_body:
                    metodo_p = str(p.get('metodo', '')).upper()
                    if metodo_p == 'CREDITO':
                        continue  # crédito no entra a caja, queda como AR
                    try:
                        monto_p = float(p.get('monto', 0))
                    except (TypeError, ValueError):
                        continue
                    if monto_p <= 0:
                        continue
                    pagos_caja.append({
                        "id": str(ObjectId()),
                        "tipo": "VENTA",
                        "monto": round(monto_p, 2),
                        "metodo": metodo_p,
                        "concepto": f"Venta {folio} ({metodo_p})",
                        "venta_id": nueva_venta["id"],
                        "venta_folio": folio,
                        "referencia": p.get('referencia', ''),
                        "fecha": datetime.utcnow().isoformat() + "Z",
                        "usuario_id": usuario_id,
                        "usuario_nombre": usuario_nombre,
                    })
                    total_contado += monto_p
                # Fallback si no había pagos[] pero sí metodo_pago top-level y no era crédito
                if not pagos_caja and metodo_pago != 'CREDITO' and total_calculado - monto_credito > 0:
                    monto_p = round(total_calculado - monto_credito, 2)
                    pagos_caja.append({
                        "id": str(ObjectId()),
                        "tipo": "VENTA",
                        "monto": monto_p,
                        "metodo": metodo_pago,
                        "concepto": f"Venta {folio} ({metodo_pago})",
                        "venta_id": nueva_venta["id"],
                        "venta_folio": folio,
                        "fecha": datetime.utcnow().isoformat() + "Z",
                        "usuario_id": usuario_id,
                        "usuario_nombre": usuario_nombre,
                    })
                    total_contado = monto_p

                if pagos_caja:
                    db.caja_sesiones.update_one(
                        {"_id": sesion["_id"]},
                        {
                            "$push": {"movimientos": {"$each": pagos_caja}},
                            "$inc": {"total_ventas": round(total_contado, 2)}
                        }
                    )
                    nueva_venta["caja_movimiento_registrado"] = True
                    # Persistir el flag en la venta para auditoría (no sólo en la respuesta JSON)
                    db["ventas"].update_one(
                        {"_id": result.inserted_id},
                        {"$set": {"caja_movimiento_registrado": True, "caja_sesion_id": str(sesion["_id"])}}
                    )
            else:
                nueva_venta["caja_movimiento_registrado"] = False
                db["ventas"].update_one(
                    {"_id": result.inserted_id},
                    {"$set": {"caja_movimiento_registrado": False}}
                )
                logger.info(f"No hay caja abierta en sucursal {sucursal_id}; movimientos no registrados.")
        except Exception as caja_err:
            logger.warning(f"No se pudo registrar movimiento en caja para venta {folio}: {caja_err}")
            nueva_venta["caja_movimiento_registrado"] = False
            try:
                db["ventas"].update_one(
                    {"_id": result.inserted_id},
                    {"$set": {"caja_movimiento_registrado": False, "caja_error": str(caja_err)}}
                )
            except Exception:
                pass

        # Limpiar para respuesta JSON
        if '_id' in nueva_venta:
            del nueva_venta['_id']

        return create_response(201, "Venta procesada con éxito", nueva_venta)

    except Exception as e:
        return handle_exception(e)


def registrar_abono_handler(event, context):
    """POST /ventas/{id}/pagos — Registra un abono contra el saldo pendiente (CxC)."""
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No autorizado")

        usuario_id = claims.get('sub', 'unknown')
        usuario_nombre = claims.get('name') or claims.get('email') or 'unknown'
        venta_id = event['pathParameters']['id']

        body = json.loads(event.get('body', '{}'))
        try:
            monto = float(body.get('monto', 0))
        except (TypeError, ValueError):
            return create_response(400, "Monto inválido")
        metodo = (body.get('metodo') or 'EFECTIVO').upper()
        referencia = body.get('referencia', '')

        if monto <= 0:
            return create_response(400, "El monto del abono debe ser mayor a cero.")

        db = get_tenant_db(tenant_id)
        venta = db["ventas"].find_one({"_id": ObjectId(venta_id)})
        if not venta:
            return create_response(404, "Venta no encontrada.")

        saldo = float(venta.get('saldo_pendiente', 0))
        if saldo <= 0:
            return create_response(400, "Esta venta no tiene saldo pendiente.")
        if monto > saldo + 0.01:  # tolerancia centavos
            return create_response(400, f"El abono (${monto:,.2f}) excede el saldo pendiente (${saldo:,.2f}).")

        nuevo_saldo = round(saldo - monto, 2)
        abono = {
            "id": str(ObjectId()),
            "monto": round(monto, 2),
            "metodo": metodo,
            "referencia": referencia,
            "fecha": datetime.utcnow().isoformat() + "Z",
            "usuario_id": usuario_id,
            "usuario_nombre": usuario_nombre,
        }

        update_doc = {
            "$push": {"abonos": abono},
            "$set": {
                "saldo_pendiente": nuevo_saldo,
                "updatedAt": datetime.utcnow()
            }
        }
        db["ventas"].update_one({"_id": ObjectId(venta_id)}, update_doc)

        # Si el abono es EFECTIVO y hay caja abierta para la sucursal, registrar movimiento
        if metodo == 'EFECTIVO':
            sesion = db.caja_sesiones.find_one({
                "sucursal_id": venta.get('sucursal_id'),
                "estado": "ABIERTA"
            })
            if sesion:
                db.caja_sesiones.update_one(
                    {"_id": sesion["_id"]},
                    {
                        "$push": {"movimientos": {
                            "id": str(ObjectId()),
                            "tipo": "ENTRADA",
                            "monto": round(monto, 2),
                            "concepto": f"Abono CxC venta {venta.get('folio')}",
                            "fecha": datetime.utcnow().isoformat() + "Z",
                            "usuario_id": usuario_id,
                            "usuario_nombre": usuario_nombre,
                        }},
                        "$inc": {"total_entradas": round(monto, 2)}
                    }
                )

        # Si saldó la OS asociada, marcarla pagada
        if nuevo_saldo == 0 and venta.get('orden_id'):
            try:
                db["ordenes_servicio"].update_one(
                    {"_id": ObjectId(venta['orden_id'])},
                    {"$set": {
                        "estado": "ENTREGADO",
                        "pagada": True,
                        "saldo_pendiente": 0,
                        "updatedAt": datetime.utcnow()
                    }}
                )
            except Exception as os_err:
                logger.warning(f"No se pudo cerrar OS {venta.get('orden_id')}: {os_err}")

        venta_actualizada = db["ventas"].find_one({"_id": ObjectId(venta_id)})
        venta_actualizada['id'] = str(venta_actualizada.pop('_id'))
        if isinstance(venta_actualizada.get('createdAt'), datetime):
            venta_actualizada['createdAt'] = venta_actualizada['createdAt'].isoformat()
        if isinstance(venta_actualizada.get('updatedAt'), datetime):
            venta_actualizada['updatedAt'] = venta_actualizada['updatedAt'].isoformat()

        return create_response(200, "Abono registrado", venta_actualizada)
    except Exception as e:
        return handle_exception(e)


def list_cxc_handler(event, context):
    """GET /ventas/cxc — Lista ventas con saldo pendiente (cuentas por cobrar)."""
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No autorizado")

        query_params = event.get('queryStringParameters') or {}
        sucursal_id = query_params.get('sucursal_id')
        cliente_id = query_params.get('cliente_id')

        query = {"saldo_pendiente": {"$gt": 0}}
        if sucursal_id:
            query["sucursal_id"] = sucursal_id
        if cliente_id:
            query["cliente_id"] = cliente_id

        db = get_tenant_db(tenant_id)
        ventas = list(db["ventas"].find(query).sort("createdAt", -1).limit(200))

        total_saldo = 0.0
        for v in ventas:
            v["id"] = str(v.pop("_id"))
            total_saldo += float(v.get('saldo_pendiente', 0))
            if isinstance(v.get('createdAt'), datetime):
                v['createdAt'] = v['createdAt'].isoformat()
            if isinstance(v.get('updatedAt'), datetime):
                v['updatedAt'] = v['updatedAt'].isoformat()

        return create_response(200, "Cuentas por cobrar", {
            "items": ventas,
            "total_saldo": round(total_saldo, 2),
            "count": len(ventas)
        })
    except Exception as e:
        return handle_exception(e)


def list_ventas_handler(event, context):
    """GET /ventas — Lista el historial de ventas (POS)."""
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id: return create_response(403, "No autorizado")

        query_params = event.get('queryStringParameters') or {}
        sucursal_id = query_params.get('sucursal_id')
        cliente_id = query_params.get('cliente_id')

        db = get_tenant_db(tenant_id)
        
        query = {"tenant_id": tenant_id}
        if sucursal_id:
            query["sucursal_id"] = sucursal_id

        if cliente_id:
            query["cliente_id"] = cliente_id

        ventas = list(db["ventas"].find(query).sort("createdAt", -1).limit(100))
        
        for v in ventas:
            v["id"] = str(v["_id"])
            del v["_id"]

        return create_response(200, "Lista de ventas obtenida", ventas)

    except Exception as e:
        return handle_exception(e)
