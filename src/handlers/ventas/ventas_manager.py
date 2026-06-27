import json
from datetime import datetime, timedelta, timezone
from aws_lambda_powertools import Logger
from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.utils.auth_utils import try_parse_id, get_claims
from src.shared.infrastructure.database import get_tenant_db, MongoDBConnection
from src.shared.utils.indexes import ensure_indexes
from src.handlers.admin.folios_manager import _get_next_folio_internal
from bson import ObjectId
from bson.errors import InvalidId
from src.shared.utils.date_utils import iso_utc

logger = Logger()

IVA_RATE = 0.16  # Tasa de IVA en México


class StockInsuficienteError(Exception):
    """Señaliza dentro de la transacción que un item perdió stock por carrera con otra venta."""
    def __init__(self, mensaje: str):
        super().__init__(mensaje)
        self.mensaje = mensaje


def _parse_fecha_cierre(value):
    """Convierte un ISO del cliente a datetime naive UTC. Devuelve None si es inválido.
    El frontend manda la fecha/hora local del cierre ya convertida a UTC (toISOString),
    así queda consistente con datetime.utcnow() del resto de las ventas."""
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).strip().replace('Z', '+00:00'))
        except (ValueError, TypeError):
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def create_venta_handler(event, context):
    """POST /ventas — Registra una venta, descuenta inventario y liga OS."""
    try:
        claims =get_claims(event)
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

        # Fecha/hora de cierre. Por defecto = ahora, pero el operador puede capturar una
        # fecha anterior cuando cierra la venta al día siguiente (la fecha afecta los
        # reportes contables, que filtran por createdAt).
        created_at = datetime.utcnow()
        fecha_cierre_in = body.get('fecha_cierre') or body.get('fecha_venta')
        if fecha_cierre_in:
            parsed = _parse_fecha_cierre(fecha_cierre_in)
            if parsed is None:
                return create_response(400, "Fecha de cierre inválida.")
            if parsed > datetime.utcnow() + timedelta(minutes=10):
                return create_response(400, "La fecha de cierre no puede ser futura.")
            if parsed < datetime.utcnow() - timedelta(days=730):
                return create_response(400, "La fecha de cierre es demasiado antigua (máximo 2 años atrás).")
            created_at = parsed

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
        for item in items:
            producto = item.get('producto', {})
            item_id = producto.get('id')
            es_externo = bool(item.get('es_externo') or producto.get('es_externo'))
            try:
                cantidad = int(item.get('cantidad', 0))
            except (TypeError, ValueError):
                cantidad = 0

            # Una pieza se salta el inventario si:
            # - Es explícitamente es_externo (traído de proveedor)
            # - El item_id es 'manual' (captura libre)
            # - No tiene item_id (emergencia)
            # - El tipo es 'SERVICIO' (mano de obra)
            is_manual_or_ext = (es_externo or not item_id or item_id == 'manual' or producto.get('tipo') == 'SERVICIO')
            
            if not is_manual_or_ext:
                 try:
                     obj_id = ObjectId(item_id)
                     p = db["items"].find_one({"_id": obj_id})
                     if p:
                         # Items con maneja_inventario:False (servicios persistidos, manuales
                         # promovidos a catálogo desde OS, etc.) no tienen stock controlado —
                         # se cobran sin validar/descontar inventario. Marcamos no_inventario
                         # para que el paso 5 tampoco intente descontar.
                         maneja_inv = p.get('maneja_inventario', True)
                         if maneja_inv:
                             if p.get('stock', 0) < cantidad:
                                  return create_response(400, f"Stock insuficiente para {producto.get('nombre')}. Disponible: {p.get('stock', 0)}")
                         else:
                             item['no_inventario'] = True
                         # Snapshot del costo para margen contable (aplica a ambos casos)
                         item['costo_unitario_snapshot'] = float(p.get('costo_promedio', p.get('precio_compra', 0)) or 0)
                     else:
                         # Si tiene ID pero no existe, lo tratamos como manual para no romper el flujo
                         item['es_externo'] = True
                         item['costo_unitario_snapshot'] = float(item.get('precio_compra', 0))
                 except (InvalidId, TypeError):
                     # ID mal formado = manual
                     item['es_externo'] = True
                     item['costo_unitario_snapshot'] = float(item.get('precio_compra', 0))
            else:
                # Caso Manual / Externo / Servicio: No valida stock.
                if 'costo_unitario_snapshot' not in item:
                    item['costo_unitario_snapshot'] = float(item.get('costo_proveedor') or item.get('precio_compra') or 0)
                item['es_externo'] = True # Aseguramos flag para el paso 5

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
            "createdAt": created_at,
            "fecha_cierre_manual": bool(fecha_cierre_in),
        }

        # 3.9 PRE-CÁLCULO DE BACKOFFICE CxP — antes de la transacción para no llamar a
        #     _get_next_folio_internal (operación atómica con su propio writeConcern) ni
        #     leer de proveedores dentro de la sesión transaccional.
        items_externos = [it for it in items if it.get('es_externo') and it.get('proveedor_id')] if orden_id else []
        por_proveedor: dict[str, list] = {}
        for it in items_externos:
            por_proveedor.setdefault(it['proveedor_id'], []).append(it)

        compras_a_insertar = []
        for p_id, lineas in por_proveedor.items():
            try:
                prov = db["proveedores"].find_one({"_id": ObjectId(p_id)})
            except (InvalidId, TypeError):
                prov = None
            if not prov:
                continue

            compra_items = []
            base_compra = 0.0
            iva_compra = 0.0
            for l in lineas:
                prod = l.get('producto', {})
                cant = int(l.get('cantidad', 1))
                costo = float(l.get('costo_proveedor') or l.get('precio_compra') or 0)
                base_ln = round(cant * costo, 2)
                iva_ln = round(base_ln * IVA_RATE, 2)
                total_ln = round(base_ln + iva_ln, 2)
                base_compra += base_ln
                iva_compra += iva_ln
                compra_items.append({
                    "item_id": prod.get('id', 'manual'),
                    "nombre": prod.get('nombre', 'Item Externo'),
                    "no_parte": prod.get('no_parte', ''),
                    "cantidad": cant,
                    "costo_unitario": costo,
                    "costo_unitario_neto": costo,
                    "costo_incluye_iva": False,
                    "iva_exento": False,
                    "subtotal_linea": base_ln,
                    "iva_linea": iva_ln,
                    "total_linea": total_ln,
                    "afecta_inventario": False
                })

            subtotal_compra = round(base_compra, 2)
            iva_total_compra = round(iva_compra, 2)
            total_compra = round(subtotal_compra + iva_total_compra, 2)

            compras_a_insertar.append({
                "folio": _get_next_folio_internal(tenant_id, "compra", sucursal_id),
                "proveedor_id": p_id,
                "proveedor_snapshot": {"id": p_id, "nombre": prov.get('nombre'), "rfc": prov.get('rfc')},
                "sucursal_id": sucursal_id,
                "fecha_factura": iso_utc(created_at),
                "items": compra_items,
                "subtotal": subtotal_compra,
                "iva": iva_total_compra,
                "descuento": 0.0,
                "total": total_compra,
                "saldo_pendiente": total_compra,
                "abonos": [],
                "estado": "RECIBIDA",
                "notas": f"Generada automáticamente desde OS {body.get('folio_orden', 'N/A')} vinculada a Venta {folio}",
                "orden_id": orden_id,
                "tenant_id": tenant_id,
                "createdAt": created_at,
            })

        # 3.95 PRE-LECTURA DE CAJA ABIERTA — leemos antes para no hacer find dentro de la transacción.
        sesion_caja = db.caja_sesiones.find_one({"sucursal_id": sucursal_id, "estado": "ABIERTA"})

        # 4-7. TRANSACCIÓN ATÓMICA: venta + stock + OS + CxP + caja.
        #      Si alguna falla, abortamos todo. Esto previene inconsistencias del tipo
        #      "venta cobrada pero stock sin descontar" o "venta sin reflejo en caja".
        client = MongoDBConnection.get_client()
        result_id = None
        with client.start_session() as session:
            try:
                with session.start_transaction():
                    # 4. Insertar Venta
                    insert_result = db["ventas"].insert_one(nueva_venta, session=session)
                    result_id = insert_result.inserted_id
                    nueva_venta["id"] = str(result_id)

                    # 5. Descontar stock (atómico + scoped por sucursal). Si la condición
                    #    {"stock": {"$gte": cantidad}} no se cumple ⇒ otro flow se llevó el stock
                    #    entre validación y descuento. Abortamos toda la venta.
                    for item in items:
                        producto = item.get('producto', {})
                        item_id = producto.get('id')
                        cantidad = int(item.get('cantidad', 0))
                        is_manual_or_ext = (item.get('es_externo') or producto.get('es_externo')
                                            or not item_id or item_id == 'manual'
                                            or producto.get('tipo') == 'SERVICIO'
                                            or item.get('no_inventario'))
                        if is_manual_or_ext:
                            continue
                        update_result = db["items"].update_one(
                            {"_id": ObjectId(item_id), "sucursal_id": sucursal_id, "stock": {"$gte": cantidad}},
                            {"$inc": {"stock": -cantidad}},
                            session=session,
                        )
                        if update_result.modified_count == 0:
                            raise StockInsuficienteError(
                                f"Stock insuficiente para {producto.get('nombre', 'item')} "
                                f"(carrera con otra venta). Venta abortada."
                            )

                    # 6. Cerrar OS + insertar compras CxP precalculadas
                    if orden_id:
                        # Snapshot del pago para la "Nota de Servicio" (PDF + pantalla).
                        # Si hubo varios métodos distintos lo marcamos MIXTO.
                        metodos_usados = {
                            str(p.get('metodo', '')).upper()
                            for p in pagos_body if p.get('metodo')
                        }
                        metodo_nota = (
                            'MIXTO' if len(metodos_usados) > 1
                            else (next(iter(metodos_usados)) if metodos_usados else metodo_pago)
                        )
                        os_update = {
                            "estado": "ENTREGADO" if monto_credito == 0 else "FINALIZADO",
                            "pagada": monto_credito == 0,
                            "venta_id": nueva_venta["id"],
                            "venta_folio": folio,
                            "saldo_pendiente": round(monto_credito, 2),
                            "pago_info": {
                                "fecha": iso_utc(),
                                "metodo": metodo_nota,
                                "venta_folio": folio,
                            },
                            "updatedAt": datetime.utcnow(),
                        }
                        db["ordenes_servicio"].update_one(
                            {"_id": ObjectId(orden_id)},
                            {"$set": os_update},
                            session=session,
                        )
                        for compra_doc in compras_a_insertar:
                            compra_doc["venta_id"] = nueva_venta["id"]
                            db["compras"].insert_one(compra_doc, session=session)

                    # 7. Caja: registrar pagos contado en sesión abierta
                    if sesion_caja:
                        pagos_caja = []
                        total_contado = 0.0
                        for p in pagos_body:
                            metodo_p = str(p.get('metodo', '')).upper()
                            if metodo_p == 'CREDITO':
                                continue
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
                                "fecha": iso_utc(),
                                "usuario_id": usuario_id,
                                "usuario_nombre": usuario_nombre,
                            })
                            total_contado += monto_p
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
                                "fecha": iso_utc(),
                                "usuario_id": usuario_id,
                                "usuario_nombre": usuario_nombre,
                            })
                            total_contado = monto_p

                        if pagos_caja:
                            db.caja_sesiones.update_one(
                                {"_id": sesion_caja["_id"]},
                                {
                                    "$push": {"movimientos": {"$each": pagos_caja}},
                                    "$inc": {"total_ventas": round(total_contado, 2)},
                                },
                                session=session,
                            )
                            db["ventas"].update_one(
                                {"_id": result_id},
                                {"$set": {
                                    "caja_movimiento_registrado": True,
                                    "caja_sesion_id": str(sesion_caja["_id"]),
                                }},
                                session=session,
                            )
                            nueva_venta["caja_movimiento_registrado"] = True
                            nueva_venta["caja_sesion_id"] = str(sesion_caja["_id"])
                    else:
                        db["ventas"].update_one(
                            {"_id": result_id},
                            {"$set": {"caja_movimiento_registrado": False}},
                            session=session,
                        )
                        nueva_venta["caja_movimiento_registrado"] = False
                        logger.info(f"No hay caja abierta en sucursal {sucursal_id}; movimientos no registrados.")
            except StockInsuficienteError as stock_err:
                logger.warning(f"Venta abortada por carrera de stock: {stock_err.mensaje}")
                return create_response(409, stock_err.mensaje)

        # Limpiar para respuesta JSON
        nueva_venta.pop('_id', None)

        return create_response(201, "Venta procesada con éxito", nueva_venta)

    except Exception as e:
        return handle_exception(e)


def registrar_abono_handler(event, context):
    """POST /ventas/{id}/pagos — Registra un abono contra el saldo pendiente (CxC)."""
    try:
        claims =get_claims(event)
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
            "fecha": iso_utc(),
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

        # Pre-lectura de caja abierta (fuera de la transacción).
        sesion_caja = None
        if metodo == 'EFECTIVO':
            sesion_caja = db.caja_sesiones.find_one({
                "sucursal_id": venta.get('sucursal_id'),
                "estado": "ABIERTA",
            })

        # Transacción atómica: abono + caja (si aplica) + cierre de OS (si saldó).
        client = MongoDBConnection.get_client()
        with client.start_session() as session:
            with session.start_transaction():
                db["ventas"].update_one({"_id": ObjectId(venta_id)}, update_doc, session=session)

                if sesion_caja:
                    db.caja_sesiones.update_one(
                        {"_id": sesion_caja["_id"]},
                        {
                            "$push": {"movimientos": {
                                "id": str(ObjectId()),
                                "tipo": "ENTRADA",
                                "monto": round(monto, 2),
                                "concepto": f"Abono CxC venta {venta.get('folio')}",
                                "fecha": iso_utc(),
                                "usuario_id": usuario_id,
                                "usuario_nombre": usuario_nombre,
                            }},
                            "$inc": {"total_entradas": round(monto, 2)}
                        },
                        session=session,
                    )

                if nuevo_saldo == 0 and venta.get('orden_id'):
                    try:
                        orden_oid = ObjectId(venta['orden_id'])
                    except (InvalidId, TypeError):
                        orden_oid = None
                    if orden_oid:
                        db["ordenes_servicio"].update_one(
                            {"_id": orden_oid},
                            {"$set": {
                                "estado": "ENTREGADO",
                                "pagada": True,
                                "saldo_pendiente": 0,
                                "updatedAt": datetime.utcnow(),
                            }},
                            session=session,
                        )

        venta_actualizada = db["ventas"].find_one({"_id": ObjectId(venta_id)})
        venta_actualizada['id'] = str(venta_actualizada.pop('_id'))
        if isinstance(venta_actualizada.get('createdAt'), datetime):
            venta_actualizada['createdAt'] = iso_utc(venta_actualizada['createdAt'])
        if isinstance(venta_actualizada.get('updatedAt'), datetime):
            venta_actualizada['updatedAt'] = iso_utc(venta_actualizada['updatedAt'])

        return create_response(200, "Abono registrado", venta_actualizada)
    except Exception as e:
        return handle_exception(e)


def list_cxc_handler(event, context):
    """GET /ventas/cxc — Lista ventas con saldo pendiente (cuentas por cobrar)."""
    try:
        claims =get_claims(event)
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
        ensure_indexes(db, tenant_id)
        ventas = list(db["ventas"].find(query).sort("createdAt", -1).limit(200))

        total_saldo = 0.0
        for v in ventas:
            v["id"] = str(v.pop("_id"))
            total_saldo += float(v.get('saldo_pendiente', 0))
            if isinstance(v.get('createdAt'), datetime):
                v['createdAt'] = iso_utc(v['createdAt'])
            if isinstance(v.get('updatedAt'), datetime):
                v['updatedAt'] = iso_utc(v['updatedAt'])

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
        claims =get_claims(event)
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id: return create_response(403, "No autorizado")

        query_params = event.get('queryStringParameters') or {}
        sucursal_id = query_params.get('sucursal_id')
        cliente_id = query_params.get('cliente_id')

        db = get_tenant_db(tenant_id)
        ensure_indexes(db, tenant_id)

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
