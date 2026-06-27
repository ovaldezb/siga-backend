"""Compras a proveedor: alta de factura recibida, recepción a inventario,
   recálculo de costo promedio ponderado, CxP y pagos a proveedor."""

import json
from datetime import datetime
from aws_lambda_powertools import Logger
from bson import ObjectId
from bson.errors import InvalidId

from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.utils.auth_utils import is_admin, get_claims
from src.shared.infrastructure.database import get_tenant_db
from src.handlers.admin.folios_manager import _get_next_folio_internal
from src.shared.utils.date_utils import iso_utc
from src.shared.utils.indexes import ensure_indexes

logger = Logger()

IVA_RATE = 0.16


def _get_claims(event):
    return get_claims(event)


def _calc_line_fiscal(precio, cantidad, incluye_iva, iva_exento):
    """Devuelve (base, iva, bruto) para una línea según flags fiscales."""
    line_amount = float(precio) * int(cantidad)
    if iva_exento:
        return line_amount, 0.0, line_amount
    if bool(incluye_iva):
        base = line_amount / (1 + IVA_RATE)
        iva = line_amount - base
        return base, iva, line_amount
    iva = line_amount * IVA_RATE
    return line_amount, iva, line_amount + iva


def _recalc_costo_promedio(stock_actual, costo_actual, cantidad_recibida, costo_unitario_neto):
    """Promedio ponderado. Si no había stock o costo, el nuevo costo es el de esta compra."""
    stock_actual = max(0, int(stock_actual or 0))
    costo_actual = float(costo_actual or 0)
    cantidad_recibida = int(cantidad_recibida or 0)
    costo_unitario_neto = float(costo_unitario_neto or 0)
    if cantidad_recibida <= 0:
        return costo_actual
    if stock_actual <= 0 or costo_actual <= 0:
        return round(costo_unitario_neto, 4)
    total = stock_actual * costo_actual + cantidad_recibida * costo_unitario_neto
    return round(total / (stock_actual + cantidad_recibida), 4)


def create_compra_handler(event, context):
    """POST /compras — Registra una factura de proveedor, suma inventario y recalcula costo promedio."""
    try:
        claims = _get_claims(event)
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No autorizado")

        usuario_id = claims.get('sub', 'unknown')
        usuario_nombre = claims.get('name') or claims.get('email') or 'unknown'

        body = json.loads(event.get('body', '{}'))
        db = get_tenant_db(tenant_id)

        proveedor_id = body.get('proveedor_id') or body.get('proveedorId')
        sucursal_id = body.get('sucursal_id') or body.get('sucursalId')
        items = body.get('items', []) or []
        referencia = (body.get('referencia') or '').strip()
        fecha_factura = body.get('fecha_factura') or iso_utc()
        notas = body.get('notas', '')

        if not proveedor_id:
            return create_response(400, "Se requiere proveedor_id.")
        if not sucursal_id:
            return create_response(400, "Se requiere sucursal_id.")
        if not items:
            return create_response(400, "Se requiere al menos un item.")

        try:
            proveedor = db.proveedores.find_one({"_id": ObjectId(proveedor_id)})
        except InvalidId:
            return create_response(400, "proveedor_id inválido.")
        if not proveedor:
            return create_response(404, "Proveedor no encontrado.")

        # Cálculo fiscal + preparación de líneas
        base_acum = 0.0
        iva_acum = 0.0
        bruto_acum = 0.0
        lineas_normalizadas = []

        for raw in items:
            item_id = raw.get('item_id') or raw.get('itemId')
            cantidad = int(raw.get('cantidad', 0) or 0)
            costo = float(raw.get('costo_unitario', 0) or 0)
            if cantidad <= 0 or costo < 0:
                continue
            incluye_iva = raw.get('costo_incluye_iva', raw.get('precio_incluye_iva', False))
            iva_exento = bool(raw.get('iva_exento', False))
            base, iva, bruto = _calc_line_fiscal(costo, cantidad, incluye_iva, iva_exento)
            costo_unitario_neto = base / cantidad if cantidad else 0.0

            linea = {
                "item_id": item_id,
                "no_parte": raw.get('no_parte') or raw.get('noParte'),
                "nombre": raw.get('nombre') or raw.get('descripcion') or '',
                "cantidad": cantidad,
                "costo_unitario": round(costo, 4),
                "costo_unitario_neto": round(costo_unitario_neto, 4),
                "costo_incluye_iva": bool(incluye_iva),
                "iva_exento": iva_exento,
                "subtotal_linea": round(base, 2),
                "iva_linea": round(iva, 2),
                "total_linea": round(bruto, 2),
                "afecta_inventario": bool(item_id and item_id != 'gasto'),
            }
            lineas_normalizadas.append(linea)
            base_acum += base
            iva_acum += iva
            bruto_acum += bruto

        if not lineas_normalizadas:
            return create_response(400, "Ninguna línea válida.")

        try:
            descuento = float(body.get('descuento', 0) or 0)
        except (TypeError, ValueError):
            descuento = 0.0
        if descuento < 0:
            descuento = 0.0

        total_bruto = max(0.0, bruto_acum - descuento)
        if bruto_acum > 0:
            factor = total_bruto / bruto_acum
            subtotal = round(base_acum * factor, 2)
            iva_total = round(iva_acum * factor, 2)
        else:
            subtotal = 0.0
            iva_total = 0.0
        total = round(subtotal + iva_total, 2)

        # Crédito / pagos iniciales
        metodo_pago = (body.get('metodo_pago') or 'EFECTIVO').upper()
        pagos_body = body.get('pagos', []) or []
        monto_credito = 0.0
        pagado_inicial = 0.0
        for p in pagos_body:
            try:
                m = float(p.get('monto', 0))
                if m <= 0:
                    continue
                if str(p.get('metodo', '')).upper() == 'CREDITO':
                    monto_credito += m
                else:
                    pagado_inicial += m
            except (TypeError, ValueError):
                continue
        if metodo_pago == 'CREDITO' and monto_credito == 0 and not pagos_body:
            monto_credito = total
        elif not pagos_body and metodo_pago != 'CREDITO':
            pagado_inicial = total

        saldo_pendiente = round(max(0.0, total - pagado_inicial), 2)

        folio = _get_next_folio_internal(tenant_id, "compra", sucursal_id)

        # Snapshot proveedor
        proveedor_snapshot = {
            "id": str(proveedor['_id']),
            "nombre": proveedor.get('nombre'),
            "rfc": proveedor.get('rfc'),
        }

        nueva_compra = {
            "folio": folio,
            "referencia_proveedor": referencia,
            "proveedor_id": proveedor_id,
            "proveedor_snapshot": proveedor_snapshot,
            "sucursal_id": sucursal_id,
            "fecha_factura": fecha_factura,
            "items": lineas_normalizadas,
            "subtotal": subtotal,
            "iva": iva_total,
            "descuento": round(descuento, 2),
            "total": total,
            "monto_credito": round(monto_credito, 2),
            "saldo_pendiente": saldo_pendiente,
            "abonos": [],
            "metodo_pago": metodo_pago,
            "pagos": pagos_body,
            "notas": notas,
            "estado": "RECIBIDA",
            "usuario_id": usuario_id,
            "usuario_nombre": usuario_nombre,
            "tenant_id": tenant_id,
            "createdAt": datetime.utcnow(),
        }

        result = db.compras.insert_one(nueva_compra)
        nueva_compra['id'] = str(result.inserted_id)

        # Aplicar a inventario: sumar stock y recalcular costo promedio
        items_warnings = []
        for linea in lineas_normalizadas:
            if not linea['afecta_inventario']:
                continue
            try:
                item_oid = ObjectId(linea['item_id'])
            except (InvalidId, TypeError):
                items_warnings.append(f"item_id inválido: {linea['item_id']}")
                continue

            item = db.items.find_one({"_id": item_oid, "sucursal_id": sucursal_id})
            if not item:
                # No existe en esta sucursal: clonar si tenemos no_parte/nombre
                if linea.get('no_parte'):
                    base_item = db.items.find_one({"no_parte": linea['no_parte'], "sucursal_id": sucursal_id})
                    if base_item:
                        item = base_item
                if not item:
                    items_warnings.append(f"Item {linea.get('nombre') or linea['item_id']} no existe en sucursal {sucursal_id}; stock no aplicado.")
                    continue

            stock_actual = int(item.get('stock', 0) or 0)
            costo_actual = float(item.get('costo_promedio', item.get('precio_compra', 0)) or 0)
            nuevo_costo = _recalc_costo_promedio(
                stock_actual, costo_actual,
                linea['cantidad'], linea['costo_unitario_neto']
            )

            db.items.update_one(
                {"_id": item['_id']},
                {
                    "$inc": {"stock": linea['cantidad']},
                    "$set": {
                        "costo_promedio": nuevo_costo,
                        "precio_compra": linea['costo_unitario'],
                        "ultima_compra_fecha": iso_utc(),
                        "ultima_compra_folio": folio,
                        "updatedAt": iso_utc(),
                    }
                }
            )

            # Bitácora
            try:
                db.inventario_movimientos.insert_one({
                    "tenant_id": tenant_id,
                    "item_id": str(item['_id']),
                    "sucursal_id": sucursal_id,
                    "cantidad": linea['cantidad'],
                    "stock_resultante": stock_actual + linea['cantidad'],
                    "concepto": "COMPRA",
                    "referencia_id": str(result.inserted_id),
                    "referencia_folio": folio,
                    "costo_unitario": linea['costo_unitario_neto'],
                    "costo_promedio_resultante": nuevo_costo,
                    "usuario_id": usuario_id,
                    "usuario_nombre": usuario_nombre,
                    "createdAt": datetime.utcnow(),
                })
            except Exception as bit_err:
                logger.warning(f"Bitácora compra: {bit_err}")

        # Caja: si hay pagos en efectivo y caja abierta, registrar SALIDA
        if pagado_inicial > 0:
            try:
                sesion = db.caja_sesiones.find_one({"sucursal_id": sucursal_id, "estado": "ABIERTA"})
                if sesion:
                    for p in pagos_body:
                        metodo_p = str(p.get('metodo', '')).upper()
                        if metodo_p in ('CREDITO',):
                            continue
                        try:
                            monto_p = float(p.get('monto', 0))
                        except (TypeError, ValueError):
                            continue
                        if monto_p <= 0:
                            continue
                        # Sólo EFECTIVO sale de caja física
                        if metodo_p == 'EFECTIVO':
                            db.caja_sesiones.update_one(
                                {"_id": sesion['_id']},
                                {
                                    "$push": {"movimientos": {
                                        "id": str(ObjectId()),
                                        "tipo": "SALIDA",
                                        "monto": round(monto_p, 2),
                                        "concepto": f"Pago compra {folio} ({proveedor_snapshot.get('nombre')})",
                                        "compra_id": nueva_compra['id'],
                                        "compra_folio": folio,
                                        "fecha": iso_utc(),
                                        "usuario_id": usuario_id,
                                        "usuario_nombre": usuario_nombre,
                                    }},
                                    "$inc": {"total_salidas": round(monto_p, 2)}
                                }
                            )
            except Exception as caja_err:
                logger.warning(f"Caja en compra: {caja_err}")

        if items_warnings:
            nueva_compra['warnings'] = items_warnings

        if '_id' in nueva_compra:
            del nueva_compra['_id']
        if isinstance(nueva_compra.get('createdAt'), datetime):
            nueva_compra['createdAt'] = iso_utc(nueva_compra['createdAt'])

        return create_response(201, "Compra registrada", nueva_compra)
    except Exception as e:
        return handle_exception(e)


def list_compras_handler(event, context):
    """GET /compras — Lista paginada con filtros (proveedor, sucursal, estado, fecha)."""
    try:
        claims = _get_claims(event)
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No autorizado")

        qp = event.get('queryStringParameters') or {}
        page = int(qp.get('page', 1))
        limit = min(int(qp.get('limit', 50)), 200)
        skip = (page - 1) * limit

        query = {}
        if qp.get('proveedor_id'):
            query['proveedor_id'] = qp['proveedor_id']
        if qp.get('sucursal_id'):
            query['sucursal_id'] = qp['sucursal_id']
        if qp.get('estado'):
            query['estado'] = qp['estado'].upper()
        if qp.get('search'):
            s = qp['search']
            query['$or'] = [
                {"folio": {"$regex": s, "$options": "i"}},
                {"referencia_proveedor": {"$regex": s, "$options": "i"}},
                {"proveedor_snapshot.nombre": {"$regex": s, "$options": "i"}},
            ]

        db = get_tenant_db(tenant_id)
        ensure_indexes(db, tenant_id)
        total = db.compras.count_documents(query)
        compras = list(db.compras.find(query).sort("createdAt", -1).skip(skip).limit(limit))
        for c in compras:
            c['id'] = str(c.pop('_id'))
            if isinstance(c.get('createdAt'), datetime):
                c['createdAt'] = iso_utc(c['createdAt'])

        return create_response(200, "Compras obtenidas", {
            "items": compras,
            "total": total,
            "page": page,
            "limit": limit
        })
    except Exception as e:
        return handle_exception(e)


def get_compra_handler(event, context):
    try:
        claims = _get_claims(event)
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No autorizado")
        compra_id = event['pathParameters']['id']
        db = get_tenant_db(tenant_id)
        try:
            compra = db.compras.find_one({"_id": ObjectId(compra_id)})
        except InvalidId:
            return create_response(400, "ID inválido.")
        if not compra:
            return create_response(404, "Compra no encontrada.")
        compra['id'] = str(compra.pop('_id'))
        if isinstance(compra.get('createdAt'), datetime):
            compra['createdAt'] = iso_utc(compra['createdAt'])
        return create_response(200, "Detalle de compra", compra)
    except Exception as e:
        return handle_exception(e)


def cancel_compra_handler(event, context):
    """POST /compras/{id}/cancel — Cancela una compra. Sólo si el stock recibido aún está disponible
       y no se han hecho abonos. Cancelaciones complejas deben usar nota de crédito manual."""
    try:
        claims = _get_claims(event)
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No autorizado")
        if not is_admin(claims):
            return create_response(403, "Sólo ADMIN puede cancelar compras.")

        compra_id = event['pathParameters']['id']
        db = get_tenant_db(tenant_id)
        compra = db.compras.find_one({"_id": ObjectId(compra_id)})
        if not compra:
            return create_response(404, "Compra no encontrada.")
        if compra.get('estado') == 'CANCELADA':
            return create_response(400, "La compra ya está cancelada.")
        if compra.get('abonos'):
            return create_response(400, "La compra tiene abonos registrados. Cancela los abonos primero o usa nota de crédito manual.")

        sucursal_id = compra.get('sucursal_id')

        # Verificar que todo el stock recibido sigue disponible
        faltantes = []
        for linea in compra.get('items', []):
            if not linea.get('afecta_inventario'):
                continue
            try:
                item = db.items.find_one({"_id": ObjectId(linea['item_id']), "sucursal_id": sucursal_id})
            except (InvalidId, TypeError):
                continue
            if not item:
                continue
            if int(item.get('stock', 0)) < int(linea['cantidad']):
                faltantes.append(f"{linea.get('nombre') or linea['item_id']}: necesita {linea['cantidad']}, disponible {item.get('stock', 0)}")

        if faltantes:
            return create_response(400, "No se puede cancelar: stock insuficiente para revertir. Detalle: " + "; ".join(faltantes))

        # Revertir stock atómicamente
        for linea in compra.get('items', []):
            if not linea.get('afecta_inventario'):
                continue
            try:
                item_oid = ObjectId(linea['item_id'])
            except (InvalidId, TypeError):
                continue
            db.items.update_one(
                {"_id": item_oid, "sucursal_id": sucursal_id, "stock": {"$gte": int(linea['cantidad'])}},
                {"$inc": {"stock": -int(linea['cantidad'])}}
            )
            try:
                db.inventario_movimientos.insert_one({
                    "tenant_id": tenant_id,
                    "item_id": linea['item_id'],
                    "sucursal_id": sucursal_id,
                    "cantidad": -int(linea['cantidad']),
                    "concepto": "CANCELACION_COMPRA",
                    "referencia_id": compra_id,
                    "referencia_folio": compra.get('folio'),
                    "usuario_id": claims.get('sub'),
                    "usuario_nombre": claims.get('name') or claims.get('email'),
                    "createdAt": datetime.utcnow(),
                })
            except Exception as bit_err:
                logger.warning(f"Bitácora cancel compra: {bit_err}")

        db.compras.update_one(
            {"_id": ObjectId(compra_id)},
            {"$set": {
                "estado": "CANCELADA",
                "fecha_cancelacion": iso_utc(),
                "cancelado_por": claims.get('name') or claims.get('email'),
                "saldo_pendiente": 0,
            }}
        )

        return create_response(200, "Compra cancelada y stock revertido.")
    except Exception as e:
        return handle_exception(e)


def registrar_pago_proveedor_handler(event, context):
    """POST /compras/{id}/pagos — Registra un pago contra el saldo pendiente (CxP)."""
    try:
        claims = _get_claims(event)
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No autorizado")

        usuario_id = claims.get('sub', 'unknown')
        usuario_nombre = claims.get('name') or claims.get('email') or 'unknown'
        compra_id = event['pathParameters']['id']

        body = json.loads(event.get('body', '{}'))
        try:
            monto = float(body.get('monto', 0))
        except (TypeError, ValueError):
            return create_response(400, "Monto inválido.")
        metodo = (body.get('metodo') or 'EFECTIVO').upper()
        referencia = body.get('referencia', '')

        if monto <= 0:
            return create_response(400, "El monto debe ser mayor a cero.")

        db = get_tenant_db(tenant_id)
        compra = db.compras.find_one({"_id": ObjectId(compra_id)})
        if not compra:
            return create_response(404, "Compra no encontrada.")
        if compra.get('estado') == 'CANCELADA':
            return create_response(400, "La compra está cancelada.")

        saldo = float(compra.get('saldo_pendiente', 0))
        if saldo <= 0:
            return create_response(400, "Esta compra no tiene saldo pendiente.")
        if monto > saldo + 0.01:
            return create_response(400, f"El pago (${monto:,.2f}) excede el saldo (${saldo:,.2f}).")

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

        db.compras.update_one(
            {"_id": ObjectId(compra_id)},
            {
                "$push": {"abonos": abono},
                "$set": {"saldo_pendiente": nuevo_saldo, "updatedAt": datetime.utcnow()}
            }
        )

        # Si EFECTIVO y hay caja abierta, registrar SALIDA
        if metodo == 'EFECTIVO':
            sesion = db.caja_sesiones.find_one({
                "sucursal_id": compra.get('sucursal_id'),
                "estado": "ABIERTA"
            })
            if sesion:
                db.caja_sesiones.update_one(
                    {"_id": sesion['_id']},
                    {
                        "$push": {"movimientos": {
                            "id": str(ObjectId()),
                            "tipo": "SALIDA",
                            "monto": round(monto, 2),
                            "concepto": f"Pago CxP compra {compra.get('folio')}",
                            "compra_id": compra_id,
                            "fecha": iso_utc(),
                            "usuario_id": usuario_id,
                            "usuario_nombre": usuario_nombre,
                        }},
                        "$inc": {"total_salidas": round(monto, 2)}
                    }
                )

        compra_actualizada = db.compras.find_one({"_id": ObjectId(compra_id)})
        compra_actualizada['id'] = str(compra_actualizada.pop('_id'))
        if isinstance(compra_actualizada.get('createdAt'), datetime):
            compra_actualizada['createdAt'] = iso_utc(compra_actualizada['createdAt'])

        return create_response(200, "Pago registrado", compra_actualizada)
    except Exception as e:
        return handle_exception(e)


def list_cxp_handler(event, context):
    """GET /compras/cxp — Lista compras con saldo pendiente (cuentas por pagar)."""
    try:
        claims = _get_claims(event)
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No autorizado")

        qp = event.get('queryStringParameters') or {}
        query = {"saldo_pendiente": {"$gt": 0}, "estado": {"$ne": "CANCELADA"}}
        if qp.get('proveedor_id'):
            query['proveedor_id'] = qp['proveedor_id']
        if qp.get('sucursal_id'):
            query['sucursal_id'] = qp['sucursal_id']

        db = get_tenant_db(tenant_id)
        compras = list(db.compras.find(query).sort("createdAt", -1).limit(200))

        total_saldo = 0.0
        for c in compras:
            c['id'] = str(c.pop('_id'))
            total_saldo += float(c.get('saldo_pendiente', 0))
            if isinstance(c.get('createdAt'), datetime):
                c['createdAt'] = iso_utc(c['createdAt'])

        return create_response(200, "Cuentas por pagar", {
            "items": compras,
            "total_saldo": round(total_saldo, 2),
            "count": len(compras)
        })
    except Exception as e:
        return handle_exception(e)
