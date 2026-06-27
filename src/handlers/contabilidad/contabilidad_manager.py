"""Reportes contables: inventario valuado, envejecimiento de CxC/CxP, margen de ventas,
resumen mensual P&L, gastos fijos mensuales e IVA mensual."""

import json
from calendar import monthrange
from datetime import datetime, timedelta
from aws_lambda_powertools import Logger
from bson import ObjectId
from bson.errors import InvalidId

from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.utils.auth_utils import is_admin, get_claims
from src.shared.infrastructure.database import get_tenant_db
from src.shared.utils.date_utils import iso_utc

logger = Logger()

IVA_RATE = 0.16  # Tasa de IVA en México (mismo valor que ventas/compras)


def _get_claims(event):
    return get_claims(event)


def _parse_date(s, default=None):
    if not s:
        return default
    try:
        if 'T' in s:
            return datetime.fromisoformat(s.replace('Z', '+00:00')).replace(tzinfo=None)
        return datetime.strptime(s[:10], '%Y-%m-%d')
    except (ValueError, TypeError):
        return default


def _bucket_age(days):
    if days <= 30:
        return '0-30'
    if days <= 60:
        return '31-60'
    if days <= 90:
        return '61-90'
    return '90+'


def get_inventario_valuado_handler(event, context):
    """GET /contabilidad/inventario-valuado?sucursal_id=..&page=..&limit=..
       Valor total = stock * costo_promedio, agrupado por item y por sucursal."""
    try:
        claims = _get_claims(event)
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No autorizado")

        qp = event.get('queryStringParameters') or {}
        sucursal_id = qp.get('sucursal_id') or qp.get('sucursalId')
        page = int(qp.get('page', 1))
        limit = min(int(qp.get('limit', 100)), 500)
        skip = (page - 1) * limit

        db = get_tenant_db(tenant_id)

        query = {"tipo": "PRODUCTO", "maneja_inventario": {"$ne": False}}
        if sucursal_id:
            query['sucursal_id'] = sucursal_id

        # Pipeline para items + cálculo de valor + agrupado por sucursal
        pipeline_items = [
            {"$match": query},
            {"$match": {"stock": {"$gt": 0}}},
            {"$addFields": {
                "costo_efectivo": {"$ifNull": ["$costo_promedio", {"$ifNull": ["$precio_compra", 0]}]},
            }},
            {"$addFields": {
                "valor_total": {"$multiply": ["$stock", "$costo_efectivo"]}
            }},
            {"$sort": {"valor_total": -1}},
            {"$skip": skip},
            {"$limit": limit},
            {"$project": {
                "_id": 1,
                "nombre": 1,
                "no_parte": 1,
                "stock": 1,
                "costo_promedio": "$costo_efectivo",
                "precio_venta": 1,
                "sucursal_id": 1,
                "valor_total": {"$round": ["$valor_total", 2]},
            }}
        ]

        # Pipeline para totales por sucursal (sin paginar)
        pipeline_totales = [
            {"$match": query},
            {"$match": {"stock": {"$gt": 0}}},
            {"$addFields": {
                "costo_efectivo": {"$ifNull": ["$costo_promedio", {"$ifNull": ["$precio_compra", 0]}]},
            }},
            {"$group": {
                "_id": "$sucursal_id",
                "valor_total": {"$sum": {"$multiply": ["$stock", "$costo_efectivo"]}},
                "unidades": {"$sum": "$stock"},
                "items_distintos": {"$sum": 1},
            }},
            {"$project": {
                "_id": 0,
                "sucursal_id": "$_id",
                "valor_total": {"$round": ["$valor_total", 2]},
                "unidades": 1,
                "items_distintos": 1,
            }}
        ]

        items = list(db.items.aggregate(pipeline_items))
        for it in items:
            it['id'] = str(it.pop('_id'))

        totales_por_sucursal = list(db.items.aggregate(pipeline_totales))
        valor_global = round(sum(t['valor_total'] for t in totales_por_sucursal), 2)

        return create_response(200, "Inventario valuado", {
            "items": items,
            "totales_por_sucursal": totales_por_sucursal,
            "valor_global": valor_global,
            "page": page,
            "limit": limit,
        })
    except Exception as e:
        return handle_exception(e)


def _aging_buckets(docs, monto_field='saldo_pendiente', fecha_field='createdAt'):
    """Agrupa documentos en buckets 0-30/31-60/61-90/90+ por antigüedad del documento."""
    now = datetime.utcnow()
    buckets = {'0-30': 0.0, '31-60': 0.0, '61-90': 0.0, '90+': 0.0}
    counts = {'0-30': 0, '31-60': 0, '61-90': 0, '90+': 0}
    for d in docs:
        fecha = d.get(fecha_field)
        if isinstance(fecha, str):
            fecha = _parse_date(fecha, now)
        if not isinstance(fecha, datetime):
            fecha = now
        days = max(0, (now - fecha).days)
        bucket = _bucket_age(days)
        saldo = float(d.get(monto_field, 0) or 0)
        buckets[bucket] += saldo
        counts[bucket] += 1
    return {
        'buckets': {k: round(v, 2) for k, v in buckets.items()},
        'counts': counts,
        'total': round(sum(buckets.values()), 2),
    }


def get_aging_cxc_handler(event, context):
    """GET /contabilidad/aging-cxc — Envejecimiento de cuentas por cobrar agrupado por cliente."""
    try:
        claims = _get_claims(event)
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No autorizado")

        qp = event.get('queryStringParameters') or {}
        query = {"saldo_pendiente": {"$gt": 0}}
        if qp.get('sucursal_id'):
            query['sucursal_id'] = qp['sucursal_id']

        db = get_tenant_db(tenant_id)
        ventas = list(db.ventas.find(query, {
            'cliente_id': 1, 'cliente_nombre': 1, 'folio': 1,
            'saldo_pendiente': 1, 'total': 1, 'createdAt': 1
        }).limit(1000))

        # Buckets globales
        global_aging = _aging_buckets(ventas)

        # Agrupado por cliente
        por_cliente = {}
        for v in ventas:
            cid = v.get('cliente_id') or 'PUBLICO_GENERAL'
            if cid not in por_cliente:
                por_cliente[cid] = {
                    'cliente_id': cid,
                    'cliente_nombre': v.get('cliente_nombre') or 'Público General',
                    'docs': []
                }
            por_cliente[cid]['docs'].append(v)

        resumen_clientes = []
        for cid, info in por_cliente.items():
            aging = _aging_buckets(info['docs'])
            resumen_clientes.append({
                'cliente_id': cid,
                'cliente_nombre': info['cliente_nombre'],
                'saldo_total': aging['total'],
                'buckets': aging['buckets'],
                'documentos': len(info['docs']),
            })
        resumen_clientes.sort(key=lambda x: x['saldo_total'], reverse=True)

        return create_response(200, "Aging CxC", {
            "global": global_aging,
            "por_cliente": resumen_clientes,
        })
    except Exception as e:
        return handle_exception(e)


def get_aging_cxp_handler(event, context):
    """GET /contabilidad/aging-cxp — Envejecimiento de cuentas por pagar agrupado por proveedor."""
    try:
        claims = _get_claims(event)
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No autorizado")

        qp = event.get('queryStringParameters') or {}
        query = {"saldo_pendiente": {"$gt": 0}, "estado": {"$ne": "CANCELADA"}}
        if qp.get('sucursal_id'):
            query['sucursal_id'] = qp['sucursal_id']

        db = get_tenant_db(tenant_id)
        compras = list(db.compras.find(query, {
            'proveedor_id': 1, 'proveedor_snapshot': 1, 'folio': 1,
            'saldo_pendiente': 1, 'total': 1, 'createdAt': 1
        }).limit(1000))

        global_aging = _aging_buckets(compras)

        por_proveedor = {}
        for c in compras:
            pid = c.get('proveedor_id') or 'SIN_PROVEEDOR'
            if pid not in por_proveedor:
                por_proveedor[pid] = {
                    'proveedor_id': pid,
                    'proveedor_nombre': (c.get('proveedor_snapshot') or {}).get('nombre') or 'Sin nombre',
                    'docs': []
                }
            por_proveedor[pid]['docs'].append(c)

        resumen = []
        for pid, info in por_proveedor.items():
            aging = _aging_buckets(info['docs'])
            resumen.append({
                'proveedor_id': pid,
                'proveedor_nombre': info['proveedor_nombre'],
                'saldo_total': aging['total'],
                'buckets': aging['buckets'],
                'documentos': len(info['docs']),
            })
        resumen.sort(key=lambda x: x['saldo_total'], reverse=True)

        return create_response(200, "Aging CxP", {
            "global": global_aging,
            "por_proveedor": resumen,
        })
    except Exception as e:
        return handle_exception(e)


def get_margen_ventas_handler(event, context):
    """GET /contabilidad/margen?desde=YYYY-MM-DD&hasta=YYYY-MM-DD&sucursal_id=..
       Margen bruto por venta basado en costo_unitario_snapshot por línea."""
    try:
        claims = _get_claims(event)
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No autorizado")

        qp = event.get('queryStringParameters') or {}
        desde = _parse_date(qp.get('desde'), datetime.utcnow() - timedelta(days=30))
        hasta = _parse_date(qp.get('hasta'), datetime.utcnow())
        # hasta inclusivo: añadimos un día
        hasta_excl = hasta + timedelta(days=1)
        sucursal_id = qp.get('sucursal_id') or qp.get('sucursalId')

        query = {"createdAt": {"$gte": desde, "$lt": hasta_excl}}
        if sucursal_id:
            query['sucursal_id'] = sucursal_id

        db = get_tenant_db(tenant_id)
        ventas = list(db.ventas.find(query, {
            'folio': 1, 'cliente_nombre': 1, 'items': 1,
            'subtotal': 1, 'total': 1, 'descuento': 1, 'sucursal_id': 1,
            'createdAt': 1,
        }))

        ingresos = 0.0
        costos = 0.0
        detalle = []
        for v in ventas:
            ingreso_neto = float(v.get('subtotal', 0) or 0)  # ya sin IVA
            costo_total = 0.0
            for it in v.get('items', []):
                try:
                    cant = int(it.get('cantidad', 0) or 0)
                except (TypeError, ValueError):
                    cant = 0
                costo_u = float(it.get('costo_unitario_snapshot', 0) or 0)
                costo_total += cant * costo_u
            ingresos += ingreso_neto
            costos += costo_total
            margen = ingreso_neto - costo_total
            margen_pct = (margen / ingreso_neto * 100) if ingreso_neto > 0 else 0
            detalle.append({
                'venta_id': str(v.get('_id')),
                'folio': v.get('folio'),
                'cliente': v.get('cliente_nombre'),
                'ingreso_neto': round(ingreso_neto, 2),
                'costo': round(costo_total, 2),
                'margen': round(margen, 2),
                'margen_pct': round(margen_pct, 2),
                'fecha': iso_utc(v.get('createdAt')) if isinstance(v.get('createdAt'), datetime) else v.get('createdAt'),
            })

        margen_total = ingresos - costos
        margen_pct_total = (margen_total / ingresos * 100) if ingresos > 0 else 0

        return create_response(200, "Margen de ventas", {
            "rango": {"desde": iso_utc(desde), "hasta": iso_utc(hasta)},
            "ingresos_netos": round(ingresos, 2),
            "costo_ventas": round(costos, 2),
            "margen_bruto": round(margen_total, 2),
            "margen_pct": round(margen_pct_total, 2),
            "ventas": len(ventas),
            "detalle": detalle[:200],
        })
    except Exception as e:
        return handle_exception(e)


# ----------------------------------------------------------------------------
# Gastos fijos mensuales: catálogo vive en configuracion.gastos_fijos_catalogo,
# instancias por mes viven en la collection `gastos_fijos_mes`.
# ----------------------------------------------------------------------------

def _parse_year_month(qp):
    """Devuelve (year, month) leyendo de query params; usa hoy si faltan o son inválidos."""
    now = datetime.utcnow()
    try:
        year = int(qp.get('year') or now.year)
    except (TypeError, ValueError):
        year = now.year
    try:
        month = int(qp.get('month') or now.month)
    except (TypeError, ValueError):
        month = now.month
    if month < 1 or month > 12:
        month = now.month
    return year, month


def _month_range(year, month):
    """Devuelve (inicio_inclusivo, fin_exclusivo) como datetime UTC para ese mes."""
    inicio = datetime(year, month, 1)
    last_day = monthrange(year, month)[1]
    fin = datetime(year, month, last_day) + timedelta(days=1)
    return inicio, fin


def _gasto_fijo_filter(year, month, sucursal_id):
    """Query para encontrar instancias del mes. Si pides sucursal_id incluye también
    los gastos generales (sucursal_id None) para no esconderlos."""
    q = {"year": int(year), "month": int(month)}
    if sucursal_id:
        q['$or'] = [
            {"sucursal_id": sucursal_id},
            {"sucursal_id": None},
            {"sucursal_id": {"$exists": False}},
        ]
    return q


def list_gastos_fijos_mes_handler(event, context):
    """GET /contabilidad/gastos-fijos?year=&month=&sucursal_id="""
    try:
        claims = _get_claims(event)
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No autorizado")

        qp = event.get('queryStringParameters') or {}
        year, month = _parse_year_month(qp)
        sucursal_id = qp.get('sucursal_id') or qp.get('sucursalId')

        db = get_tenant_db(tenant_id)
        docs = list(db.gastos_fijos_mes.find(_gasto_fijo_filter(year, month, sucursal_id))
                    .sort([("categoria", 1), ("concepto_nombre", 1)]))

        items = []
        total_estimado = 0.0
        total_real = 0.0
        total_pagado = 0.0
        pendientes = 0
        for d in docs:
            d['id'] = str(d.pop('_id'))
            for k in ('createdAt', 'updatedAt', 'fecha_pago'):
                v = d.get(k)
                if isinstance(v, datetime):
                    d[k] = iso_utc(v)
            estimado = float(d.get('monto_estimado') or 0)
            real = float(d.get('monto_real') or 0)
            total_estimado += estimado
            total_real += real
            if (d.get('status') or 'PENDIENTE').upper() == 'PAGADO':
                total_pagado += real
            else:
                pendientes += 1
            items.append(d)

        return create_response(200, "Gastos fijos del mes", {
            "year": year,
            "month": month,
            "items": items,
            "totales": {
                "estimado": round(total_estimado, 2),
                "real": round(total_real, 2),
                "pagado": round(total_pagado, 2),
                "pendientes": pendientes,
                "count": len(items),
            }
        })
    except Exception as e:
        return handle_exception(e)


def seed_gastos_fijos_mes_handler(event, context):
    """POST /contabilidad/gastos-fijos/seed  body: {year, month, sucursal_id?}
    Crea una instancia por cada concepto activo del catálogo que aún no exista para
    ese mes/sucursal. Idempotente: si ya hay instancia para concepto+mes+sucursal, no la duplica."""
    try:
        claims = _get_claims(event)
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No autorizado")

        body = json.loads(event.get('body', '{}'))
        year = int(body.get('year') or datetime.utcnow().year)
        month = int(body.get('month') or datetime.utcnow().month)
        sucursal_id = body.get('sucursal_id') or body.get('sucursalId') or None

        if month < 1 or month > 12:
            return create_response(400, "Mes inválido.")

        db = get_tenant_db(tenant_id)
        config = db.configuracion.find_one({"tenant_id": tenant_id}) or {}
        catalogo = config.get('gastos_fijos_catalogo') or []
        activos = [c for c in catalogo if c.get('activo', True)]

        if not activos:
            return create_response(400, "El catálogo de gastos fijos está vacío. Agrega conceptos en Configuración.")

        creados = 0
        existentes = 0
        usuario_nombre = claims.get('name') or claims.get('email') or 'unknown'
        now = datetime.utcnow()

        for c in activos:
            concepto_id = c.get('id') or c.get('nombre')
            ya = db.gastos_fijos_mes.find_one({
                "year": year, "month": month,
                "concepto_id": concepto_id,
                "sucursal_id": sucursal_id,
            })
            if ya:
                existentes += 1
                continue
            db.gastos_fijos_mes.insert_one({
                "tenant_id": tenant_id,
                "year": year,
                "month": month,
                "concepto_id": concepto_id,
                "concepto_nombre": c.get('nombre'),
                "categoria": c.get('categoria') or '',
                "icono": c.get('icono') or '',
                "monto_estimado": float(c.get('monto_estimado') or 0),
                "monto_real": 0.0,
                "fecha_pago": None,
                "status": "PENDIENTE",
                "notas": "",
                "sucursal_id": sucursal_id,
                "createdAt": now,
                "updatedAt": now,
                "createdBy": usuario_nombre,
            })
            creados += 1

        return create_response(200, f"Sembrado: {creados} nuevos, {existentes} ya existían.", {
            "creados": creados,
            "existentes": existentes,
            "year": year,
            "month": month,
        })
    except Exception as e:
        return handle_exception(e)


def upsert_gasto_fijo_mes_handler(event, context):
    """POST /contabilidad/gastos-fijos  body: {id?, year, month, concepto_id, concepto_nombre,
       categoria?, monto_estimado, monto_real, fecha_pago?, status, notas?, sucursal_id?}.
       Si trae id actualiza; si no, crea."""
    try:
        claims = _get_claims(event)
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No autorizado")

        body = json.loads(event.get('body', '{}'))
        db = get_tenant_db(tenant_id)
        usuario_nombre = claims.get('name') or claims.get('email') or 'unknown'

        # Normalizar campos editables
        editable = {}
        if 'concepto_id' in body:       editable['concepto_id'] = body.get('concepto_id') or ''
        if 'concepto_nombre' in body:   editable['concepto_nombre'] = body.get('concepto_nombre') or ''
        if 'categoria' in body:         editable['categoria'] = body.get('categoria') or ''
        if 'icono' in body:             editable['icono'] = body.get('icono') or ''
        if 'monto_estimado' in body:    editable['monto_estimado'] = float(body.get('monto_estimado') or 0)
        if 'monto_real' in body:        editable['monto_real'] = float(body.get('monto_real') or 0)
        if 'fecha_pago' in body:        editable['fecha_pago'] = body.get('fecha_pago') or None
        if 'status' in body:            editable['status'] = (body.get('status') or 'PENDIENTE').upper()
        if 'notas' in body:             editable['notas'] = body.get('notas') or ''
        if 'sucursal_id' in body:       editable['sucursal_id'] = body.get('sucursal_id') or None

        gasto_id = body.get('id')
        if gasto_id:
            try:
                oid = ObjectId(gasto_id)
            except InvalidId:
                return create_response(400, "id inválido.")
            editable['updatedAt'] = datetime.utcnow()
            editable['updatedBy'] = usuario_nombre
            res = db.gastos_fijos_mes.update_one({"_id": oid, "tenant_id": tenant_id}, {"$set": editable})
            if res.matched_count == 0:
                return create_response(404, "Gasto no encontrado.")
            doc = db.gastos_fijos_mes.find_one({"_id": oid})
        else:
            # creación: requiere year/month/concepto_nombre
            if not body.get('year') or not body.get('month'):
                return create_response(400, "year y month son obligatorios.")
            doc_new = {
                "tenant_id": tenant_id,
                "year": int(body['year']),
                "month": int(body['month']),
                "concepto_id": editable.get('concepto_id') or (body.get('concepto_nombre') or 'gasto').lower().replace(' ', '_'),
                "concepto_nombre": editable.get('concepto_nombre') or 'Gasto',
                "categoria": editable.get('categoria', ''),
                "icono": editable.get('icono', ''),
                "monto_estimado": editable.get('monto_estimado', 0.0),
                "monto_real": editable.get('monto_real', 0.0),
                "fecha_pago": editable.get('fecha_pago'),
                "status": editable.get('status', 'PENDIENTE'),
                "notas": editable.get('notas', ''),
                "sucursal_id": editable.get('sucursal_id'),
                "createdAt": datetime.utcnow(),
                "updatedAt": datetime.utcnow(),
                "createdBy": usuario_nombre,
            }
            inserted = db.gastos_fijos_mes.insert_one(doc_new)
            doc = db.gastos_fijos_mes.find_one({"_id": inserted.inserted_id})

        doc['id'] = str(doc.pop('_id'))
        for k in ('createdAt', 'updatedAt'):
            if isinstance(doc.get(k), datetime):
                doc[k] = iso_utc(doc[k])
        return create_response(200, "Gasto guardado", doc)
    except Exception as e:
        return handle_exception(e)


def delete_gasto_fijo_mes_handler(event, context):
    """DELETE /contabilidad/gastos-fijos/{id}"""
    try:
        claims = _get_claims(event)
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No autorizado")

        gasto_id = (event.get('pathParameters') or {}).get('id')
        try:
            oid = ObjectId(gasto_id)
        except (InvalidId, TypeError):
            return create_response(400, "id inválido.")

        db = get_tenant_db(tenant_id)
        res = db.gastos_fijos_mes.delete_one({"_id": oid, "tenant_id": tenant_id})
        if res.deleted_count == 0:
            return create_response(404, "Gasto no encontrado.")
        return create_response(200, "Gasto eliminado.")
    except Exception as e:
        return handle_exception(e)


# ----------------------------------------------------------------------------
# Gastos variables (operativos manuales): gastos del día a día del taller que NO
# son costo de venta ni compras a inventario (combustible, viáticos, comisiones,
# papelería, mantenimiento menor…). Viven en la collection `gastos_variables`,
# completamente separados de `compras` (costo de ventas) y `gastos_fijos_mes`.
# ----------------------------------------------------------------------------

def _gasto_variable_filter(year, month, sucursal_id):
    """Query por mes. Si pides sucursal_id incluye también los gastos generales
    (sucursal_id None/ausente) para no esconderlos."""
    q = {"year": int(year), "month": int(month)}
    if sucursal_id:
        q['$or'] = [
            {"sucursal_id": sucursal_id},
            {"sucursal_id": None},
            {"sucursal_id": {"$exists": False}},
        ]
    return q


def list_gastos_variables_handler(event, context):
    """GET /contabilidad/gastos-variables?year=&month=&sucursal_id="""
    try:
        claims = _get_claims(event)
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No autorizado")

        qp = event.get('queryStringParameters') or {}
        year, month = _parse_year_month(qp)
        sucursal_id = qp.get('sucursal_id') or qp.get('sucursalId')

        db = get_tenant_db(tenant_id)
        docs = list(db.gastos_variables.find(_gasto_variable_filter(year, month, sucursal_id))
                    .sort([("fecha", -1), ("createdAt", -1)]))

        items = []
        total = 0.0
        for d in docs:
            d['id'] = str(d.pop('_id'))
            for k in ('fecha', 'createdAt', 'updatedAt'):
                v = d.get(k)
                if isinstance(v, datetime):
                    d[k] = iso_utc(v)
            total += float(d.get('monto') or 0)
            items.append(d)

        return create_response(200, "Gastos variables del mes", {
            "year": year,
            "month": month,
            "items": items,
            "totales": {
                "monto": round(total, 2),
                "count": len(items),
            }
        })
    except Exception as e:
        return handle_exception(e)


def upsert_gasto_variable_handler(event, context):
    """POST /contabilidad/gastos-variables  body: {id?, fecha, concepto, categoria?,
       monto, metodo_pago?, notas?, sucursal_id?}. Si trae id actualiza; si no, crea.
       year/month se derivan de `fecha` (o del par year/month explícito)."""
    try:
        claims = _get_claims(event)
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No autorizado")

        body = json.loads(event.get('body', '{}'))
        db = get_tenant_db(tenant_id)
        usuario_nombre = claims.get('name') or claims.get('email') or 'unknown'
        now = datetime.utcnow()

        # Normalizar campos editables
        editable = {}
        fecha_dt = None
        if 'fecha' in body:
            fecha_dt = _parse_date(body.get('fecha'))
            if fecha_dt is None:
                return create_response(400, "fecha inválida (usa YYYY-MM-DD).")
            editable['fecha'] = fecha_dt
            editable['year'] = fecha_dt.year
            editable['month'] = fecha_dt.month
        if 'concepto' in body:    editable['concepto'] = (body.get('concepto') or '').strip()
        if 'categoria' in body:   editable['categoria'] = (body.get('categoria') or '').strip()
        if 'monto' in body:       editable['monto'] = float(body.get('monto') or 0)
        if 'metodo_pago' in body: editable['metodo_pago'] = (body.get('metodo_pago') or '').strip()
        if 'notas' in body:       editable['notas'] = (body.get('notas') or '').strip()
        if 'sucursal_id' in body: editable['sucursal_id'] = body.get('sucursal_id') or None

        gasto_id = body.get('id')
        if gasto_id:
            try:
                oid = ObjectId(gasto_id)
            except InvalidId:
                return create_response(400, "id inválido.")
            editable['updatedAt'] = now
            editable['updatedBy'] = usuario_nombre
            res = db.gastos_variables.update_one({"_id": oid, "tenant_id": tenant_id}, {"$set": editable})
            if res.matched_count == 0:
                return create_response(404, "Gasto no encontrado.")
            doc = db.gastos_variables.find_one({"_id": oid})
        else:
            # creación: requiere concepto + monto + fecha
            if not editable.get('concepto'):
                return create_response(400, "El concepto es obligatorio.")
            if fecha_dt is None:
                # Si no mandaron fecha, usar hoy.
                fecha_dt = now
                editable['fecha'] = fecha_dt
                editable['year'] = fecha_dt.year
                editable['month'] = fecha_dt.month
            doc_new = {
                "tenant_id": tenant_id,
                "fecha": editable['fecha'],
                "year": editable['year'],
                "month": editable['month'],
                "concepto": editable.get('concepto'),
                "categoria": editable.get('categoria', ''),
                "monto": editable.get('monto', 0.0),
                "metodo_pago": editable.get('metodo_pago', ''),
                "notas": editable.get('notas', ''),
                "sucursal_id": editable.get('sucursal_id'),
                "createdAt": now,
                "updatedAt": now,
                "createdBy": usuario_nombre,
            }
            inserted = db.gastos_variables.insert_one(doc_new)
            doc = db.gastos_variables.find_one({"_id": inserted.inserted_id})

        doc['id'] = str(doc.pop('_id'))
        for k in ('fecha', 'createdAt', 'updatedAt'):
            if isinstance(doc.get(k), datetime):
                doc[k] = iso_utc(doc[k])
        return create_response(200, "Gasto variable guardado", doc)
    except Exception as e:
        return handle_exception(e)


def delete_gasto_variable_handler(event, context):
    """DELETE /contabilidad/gastos-variables/{id}"""
    try:
        claims = _get_claims(event)
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No autorizado")

        gasto_id = (event.get('pathParameters') or {}).get('id')
        try:
            oid = ObjectId(gasto_id)
        except (InvalidId, TypeError):
            return create_response(400, "id inválido.")

        db = get_tenant_db(tenant_id)
        res = db.gastos_variables.delete_one({"_id": oid, "tenant_id": tenant_id})
        if res.deleted_count == 0:
            return create_response(404, "Gasto no encontrado.")
        return create_response(200, "Gasto eliminado.")
    except Exception as e:
        return handle_exception(e)


# ----------------------------------------------------------------------------
# Resumen mensual (P&L) e IVA mensual
# ----------------------------------------------------------------------------

def _is_venta_valida(v):
    """Excluye ventas canceladas / anuladas del cálculo de P&L."""
    estado = (v.get('estado') or '').upper()
    return estado not in ('CANCELADA', 'ANULADA')


def get_resumen_mensual_handler(event, context):
    """GET /contabilidad/resumen-mensual?year=&month=&sucursal_id=
    Devuelve P&L del mes: ingresos, costo de venta, gastos variables (compras sin inventario),
    gastos fijos, utilidad bruta, utilidad neta. Detalle de OS y de gastos variables."""
    try:
        claims = _get_claims(event)
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No autorizado")

        qp = event.get('queryStringParameters') or {}
        year, month = _parse_year_month(qp)
        sucursal_id = qp.get('sucursal_id') or qp.get('sucursalId')
        desde, hasta_excl = _month_range(year, month)

        db = get_tenant_db(tenant_id)

        # --- Ventas del mes ---
        q_ventas = {"createdAt": {"$gte": desde, "$lt": hasta_excl}}
        if sucursal_id:
            q_ventas['sucursal_id'] = sucursal_id
        ventas = list(db.ventas.find(q_ventas, {
            'folio': 1, 'cliente_nombre': 1, 'cliente_id': 1,
            'items': 1, 'subtotal': 1, 'iva': 1, 'total': 1, 'descuento': 1,
            'orden_id': 1, 'sucursal_id': 1, 'createdAt': 1, 'estado': 1,
        }))

        ingresos_brutos = 0.0   # total con IVA
        ingresos_netos = 0.0    # subtotal sin IVA
        iva_cobrado = 0.0
        costo_venta = 0.0
        ventas_detalle = []
        os_detalle = []

        for v in ventas:
            if not _is_venta_valida(v):
                continue
            sub = float(v.get('subtotal') or 0)
            iva_v = float(v.get('iva') or 0)
            tot = float(v.get('total') or (sub + iva_v))
            ingresos_brutos += tot
            ingresos_netos += sub
            iva_cobrado += iva_v

            costo_v = 0.0
            for it in v.get('items') or []:
                try:
                    cant = int(it.get('cantidad') or 0)
                except (TypeError, ValueError):
                    cant = 0
                costo_u = float(it.get('costo_unitario_snapshot') or 0)
                costo_v += cant * costo_u
            costo_venta += costo_v

            margen = sub - costo_v
            fecha_iso = v.get('createdAt')
            if isinstance(fecha_iso, datetime):
                fecha_iso = iso_utc(fecha_iso)
            row = {
                'venta_id': str(v.get('_id')),
                'folio': v.get('folio'),
                'cliente': v.get('cliente_nombre'),
                'cliente_id': v.get('cliente_id'),
                'orden_id': v.get('orden_id'),
                'ingreso_neto': round(sub, 2),
                'iva': round(iva_v, 2),
                'ingreso_bruto': round(tot, 2),
                'costo': round(costo_v, 2),
                'margen': round(margen, 2),
                'fecha': fecha_iso,
            }
            ventas_detalle.append(row)
            if v.get('orden_id'):
                os_detalle.append(row)

        # --- Compras del mes: separar gastos variables (sin inventario) e IVA acreditable ---
        q_compras = {
            "createdAt": {"$gte": desde, "$lt": hasta_excl},
            "estado": {"$ne": "CANCELADA"},
        }
        if sucursal_id:
            q_compras['sucursal_id'] = sucursal_id
        compras = list(db.compras.find(q_compras, {
            'folio': 1, 'proveedor_snapshot': 1, 'proveedor_id': 1,
            'items': 1, 'subtotal': 1, 'iva': 1, 'total': 1,
            'sucursal_id': 1, 'createdAt': 1, 'fecha_factura': 1, 'estado': 1,
            'notas': 1,
        }))

        gastos_variables = 0.0       # subtotal (base sin IVA) de líneas sin inventario
        iva_acreditable = 0.0        # iva de todas las compras del mes (incluye inventario)
        compras_inventario_base = 0.0  # base de líneas con inventario (no es gasto del mes, va a inventario)
        gastos_variables_detalle = []

        for c in compras:
            iva_acreditable += float(c.get('iva') or 0)
            for ln in c.get('items') or []:
                base_ln = float(ln.get('subtotal_linea') or 0)
                iva_ln = float(ln.get('iva_linea') or 0)
                if not ln.get('afecta_inventario'):
                    # Gasto puro: cuenta como egreso variable del mes
                    gastos_variables += base_ln
                    fecha_iso = c.get('createdAt')
                    if isinstance(fecha_iso, datetime):
                        fecha_iso = iso_utc(fecha_iso)
                    gastos_variables_detalle.append({
                        'compra_id': str(c.get('_id')),
                        'folio': c.get('folio'),
                        'proveedor': (c.get('proveedor_snapshot') or {}).get('nombre'),
                        'proveedor_id': c.get('proveedor_id'),
                        'concepto': ln.get('nombre') or 'Gasto',
                        'cantidad': ln.get('cantidad'),
                        'base': round(base_ln, 2),
                        'iva': round(iva_ln, 2),
                        'total': round(base_ln + iva_ln, 2),
                        'fecha': fecha_iso,
                    })
                else:
                    compras_inventario_base += base_ln

        # --- Gastos fijos del mes ---
        docs_fijos = list(db.gastos_fijos_mes.find(_gasto_fijo_filter(year, month, sucursal_id)))
        gastos_fijos_real = 0.0
        gastos_fijos_estimado = 0.0
        gastos_fijos_pagado = 0.0
        gastos_fijos_detalle = []
        for d in docs_fijos:
            estimado = float(d.get('monto_estimado') or 0)
            real = float(d.get('monto_real') or 0)
            status = (d.get('status') or 'PENDIENTE').upper()
            gastos_fijos_estimado += estimado
            gastos_fijos_real += real
            if status == 'PAGADO':
                gastos_fijos_pagado += real
            gastos_fijos_detalle.append({
                'id': str(d.get('_id')),
                'concepto': d.get('concepto_nombre'),
                'categoria': d.get('categoria'),
                'estimado': round(estimado, 2),
                'real': round(real, 2),
                'status': status,
                'fecha_pago': d.get('fecha_pago'),
            })

        # --- Gastos variables manuales del mes (collection gastos_variables) ---
        # Gastos operativos capturados a mano, separados de compras (costo de venta)
        # y de gastos fijos. Restan de la utilidad neta como egreso del mes.
        gastos_variables_manuales = 0.0
        gastos_variables_manuales_detalle = []
        for d in db.gastos_variables.find(_gasto_variable_filter(year, month, sucursal_id)):
            monto = float(d.get('monto') or 0)
            gastos_variables_manuales += monto
            fecha_v = d.get('fecha')
            gastos_variables_manuales_detalle.append({
                'id': str(d.get('_id')),
                'fecha': iso_utc(fecha_v) if isinstance(fecha_v, datetime) else fecha_v,
                'concepto': d.get('concepto') or 'Gasto',
                'categoria': d.get('categoria') or '',
                'metodo_pago': d.get('metodo_pago') or '',
                'monto': round(monto, 2),
            })

        # --- Cálculos finales ---
        # Utilidad bruta = ingresos netos - costo de venta
        utilidad_bruta = ingresos_netos - costo_venta
        # Utilidad operativa = bruta - gastos variables (compras sin inventario)
        #   - gastos variables manuales - gastos fijos REAL (devengados).
        utilidad_neta = utilidad_bruta - gastos_variables - gastos_variables_manuales - gastos_fijos_real

        margen_bruto_pct = (utilidad_bruta / ingresos_netos * 100) if ingresos_netos > 0 else 0.0
        margen_neto_pct = (utilidad_neta / ingresos_netos * 100) if ingresos_netos > 0 else 0.0

        return create_response(200, "Resumen mensual", {
            "year": year,
            "month": month,
            "sucursal_id": sucursal_id,
            "rango": {"desde": iso_utc(desde), "hasta": iso_utc(hasta_excl)},
            "ingresos": {
                "brutos": round(ingresos_brutos, 2),
                "netos": round(ingresos_netos, 2),
                "iva_cobrado": round(iva_cobrado, 2),
                "ventas_count": len(ventas_detalle),
                "os_count": len(os_detalle),
            },
            "costo_venta": round(costo_venta, 2),
            "gastos_variables": round(gastos_variables, 2),
            "gastos_variables_manuales": round(gastos_variables_manuales, 2),
            "gastos_fijos": {
                "estimado": round(gastos_fijos_estimado, 2),
                "real": round(gastos_fijos_real, 2),
                "pagado": round(gastos_fijos_pagado, 2),
                "count": len(gastos_fijos_detalle),
            },
            "compras_inventario_base": round(compras_inventario_base, 2),
            "utilidad_bruta": round(utilidad_bruta, 2),
            "utilidad_neta": round(utilidad_neta, 2),
            "margen_bruto_pct": round(margen_bruto_pct, 2),
            "margen_neto_pct": round(margen_neto_pct, 2),
            "iva": {
                "cobrado": round(iva_cobrado, 2),
                "acreditable": round(iva_acreditable, 2),
                "saldo": round(iva_cobrado - iva_acreditable, 2),
            },
            "detalle": {
                "ventas": ventas_detalle[:500],
                "ordenes_servicio": os_detalle[:500],
                "gastos_variables": gastos_variables_detalle[:500],
                "gastos_variables_manuales": gastos_variables_manuales_detalle[:500],
                "gastos_fijos": gastos_fijos_detalle,
            },
        })
    except Exception as e:
        return handle_exception(e)


def get_resumen_por_os_handler(event, context):
    """GET /contabilidad/resumen-os?year=&month=&sucursal_id=

    Devuelve una fila por OS facturada en el mes con:
      - ingresos_netos (subtotal de la venta)
      - costo_inventario  (líneas de items propios — costo_unitario_snapshot × cantidad)
      - costo_externo     (líneas con es_externo=True — costo proveedor × cantidad)
      - costo_regalado    (líneas con no_cobrar/cortesía en la OS original — costo × cantidad)
                           Se RESTA de la utilidad: el taller pagó la pieza aunque no la cobró.
      - proveedores       [{proveedor_id, proveedor_nombre, total, items_count}]
      - utilidad_neta = ingresos_netos − costo_inventario − costo_externo − costo_regalado

    Necesita leer la OS asociada para detectar items con no_cobrar (cortesía), porque en la
    venta llegan a $0 y su costo se contaría como parte del COGS general — aquí lo aislamos
    para que el operador vea explícitamente el impacto de los regalos.
    """
    try:
        claims = _get_claims(event)
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No autorizado")

        qp = event.get('queryStringParameters') or {}
        year, month = _parse_year_month(qp)
        sucursal_id = qp.get('sucursal_id') or qp.get('sucursalId')
        desde, hasta_excl = _month_range(year, month)

        db = get_tenant_db(tenant_id)

        # Sólo ventas que tienen OS asociada (es decir, no ventas de mostrador puro).
        q_ventas = {
            "createdAt": {"$gte": desde, "$lt": hasta_excl},
            "orden_id": {"$nin": [None, ""]},
            "estado": {"$nin": ["CANCELADA", "ANULADA"]},
        }
        if sucursal_id:
            q_ventas['sucursal_id'] = sucursal_id
        ventas = list(db.ventas.find(q_ventas, {
            'folio': 1, 'cliente_nombre': 1, 'cliente_id': 1,
            'items': 1, 'subtotal': 1, 'iva': 1, 'total': 1, 'descuento': 1,
            'orden_id': 1, 'sucursal_id': 1, 'createdAt': 1, 'estado': 1,
            'vehiculo_snapshot': 1,
        }))

        # Pre-cargar las OS de un golpe para no hacer N+1.
        orden_ids = []
        for v in ventas:
            oid = v.get('orden_id')
            if oid:
                try:
                    orden_ids.append(ObjectId(oid))
                except (InvalidId, TypeError):
                    pass
        ordenes_map = {}
        if orden_ids:
            for o in db.ordenes_servicio.find({"_id": {"$in": orden_ids}},
                                              {'folio': 1, 'puntosArreglar': 1, 'cliente_snapshot': 1,
                                               'vehiculo_snapshot': 1, 'mecanico_nombre': 1}):
                ordenes_map[str(o['_id'])] = o

        filas = []
        tot_ingreso = 0.0
        tot_costo_inv = 0.0
        tot_costo_ext = 0.0
        tot_costo_reg = 0.0
        proveedores_global = {}

        for v in ventas:
            ingreso_neto = float(v.get('subtotal') or 0)
            costo_inv = 0.0
            costo_ext = 0.0
            proveedores_os = {}  # proveedor_id -> {nombre, total, items}

            # Costo de la venta: leer items[] tal cual quedaron snapshotted.
            for it in v.get('items') or []:
                try:
                    cant = int(it.get('cantidad') or 0)
                except (TypeError, ValueError):
                    cant = 0
                costo_u = float(it.get('costo_unitario_snapshot') or 0)
                producto = it.get('producto') or {}
                es_externo = bool(it.get('es_externo') or producto.get('es_externo'))
                linea = round(costo_u * cant, 2)

                if es_externo:
                    costo_ext += linea
                    prov_id = it.get('proveedor_id') or producto.get('proveedor_id') or 'sin-proveedor'
                    prov_nombre = (it.get('proveedor_nombre')
                                   or producto.get('proveedor_nombre')
                                   or 'Sin proveedor')
                    bucket = proveedores_os.setdefault(prov_id, {
                        'proveedor_id': prov_id,
                        'proveedor_nombre': prov_nombre,
                        'total': 0.0,
                        'items_count': 0,
                    })
                    bucket['total'] = round(bucket['total'] + linea, 2)
                    bucket['items_count'] += 1
                    # Global breakdown
                    g = proveedores_global.setdefault(prov_id, {
                        'proveedor_id': prov_id,
                        'proveedor_nombre': prov_nombre,
                        'total': 0.0,
                        'items_count': 0,
                        'os_count': 0,
                    })
                    g['total'] = round(g['total'] + linea, 2)
                    g['items_count'] += 1
                else:
                    costo_inv += linea

            # Regalos: leer la OS original para detectar items con no_cobrar (su costo SÍ
            # afectó utilidad porque se descontó stock / se pagó al proveedor, pero el
            # cliente no los pagó).
            costo_reg = 0.0
            regalados_detalle = []
            orden_doc = ordenes_map.get(v.get('orden_id'))
            if orden_doc:
                for punto in (orden_doc.get('puntosArreglar') or []):
                    for it_os in (punto.get('items') or []):
                        if not it_os.get('no_cobrar'):
                            continue
                        try:
                            cant = float(it_os.get('piezas') or 0)
                        except (TypeError, ValueError):
                            cant = 0
                        # Usa costo_proveedor si es externo, si no precioCompra.
                        if it_os.get('es_externo'):
                            costo_u = float(it_os.get('costo_proveedor') or it_os.get('precioCompra') or 0)
                        else:
                            costo_u = float(it_os.get('precioCompra') or 0)
                        linea_reg = round(costo_u * cant, 2)
                        costo_reg += linea_reg
                        regalados_detalle.append({
                            'nombre': it_os.get('nombre'),
                            'cantidad': cant,
                            'costo_unitario': round(costo_u, 2),
                            'total': linea_reg,
                            'punto': punto.get('nombre'),
                            'es_externo': bool(it_os.get('es_externo')),
                        })

            utilidad_neta = round(ingreso_neto - costo_inv - costo_ext - costo_reg, 2)
            tot_ingreso += ingreso_neto
            tot_costo_inv += costo_inv
            tot_costo_ext += costo_ext
            tot_costo_reg += costo_reg
            if orden_doc:
                # contar OS distintas (los proveedores_os los acumulamos a nivel OS)
                for pid in proveedores_os:
                    proveedores_global[pid]['os_count'] += 1

            fecha_iso = v.get('createdAt')
            if isinstance(fecha_iso, datetime):
                fecha_iso = iso_utc(fecha_iso)

            vehiculo = v.get('vehiculo_snapshot') or (orden_doc.get('vehiculo_snapshot') if orden_doc else None) or {}
            cliente_snap = orden_doc.get('cliente_snapshot') if orden_doc else None
            cliente_nombre = v.get('cliente_nombre') or (
                f"{cliente_snap.get('nombre','')} {cliente_snap.get('apellido_paterno','')}".strip()
                if cliente_snap else 'Sin cliente'
            )

            filas.append({
                'venta_id': str(v.get('_id')),
                'venta_folio': v.get('folio'),
                'orden_id': v.get('orden_id'),
                'orden_folio': orden_doc.get('folio') if orden_doc else None,
                'cliente': cliente_nombre,
                'vehiculo': {
                    'placas': vehiculo.get('placas'),
                    'marca': vehiculo.get('marca'),
                    'modelo': vehiculo.get('modelo'),
                },
                'mecanico': orden_doc.get('mecanico_nombre') if orden_doc else None,
                'fecha': fecha_iso,
                'ingreso_neto': round(ingreso_neto, 2),
                'costo_inventario': round(costo_inv, 2),
                'costo_externo': round(costo_ext, 2),
                'costo_regalado': round(costo_reg, 2),
                'utilidad_neta': utilidad_neta,
                'margen_pct': round((utilidad_neta / ingreso_neto * 100), 2) if ingreso_neto > 0 else 0.0,
                'proveedores': list(proveedores_os.values()),
                'regalados': regalados_detalle,
            })

        filas.sort(key=lambda r: r.get('fecha') or '', reverse=True)

        utilidad_total = round(tot_ingreso - tot_costo_inv - tot_costo_ext - tot_costo_reg, 2)

        return create_response(200, "Resumen por OS", {
            'year': year,
            'month': month,
            'sucursal_id': sucursal_id,
            'rango': {"desde": iso_utc(desde), "hasta": iso_utc(hasta_excl)},
            'totales': {
                'ingreso_neto': round(tot_ingreso, 2),
                'costo_inventario': round(tot_costo_inv, 2),
                'costo_externo': round(tot_costo_ext, 2),
                'costo_regalado': round(tot_costo_reg, 2),
                'utilidad_neta': utilidad_total,
                'margen_pct': round((utilidad_total / tot_ingreso * 100), 2) if tot_ingreso > 0 else 0.0,
                'os_count': len(filas),
            },
            'proveedores_global': sorted(proveedores_global.values(), key=lambda x: x['total'], reverse=True),
            'ordenes': filas,
        })
    except Exception as e:
        return handle_exception(e)


def get_concentrado_ventas_handler(event, context):
    """GET /contabilidad/concentrado-ventas?year=&month=&sucursal_id=

    Concentrado a nivel RENGLÓN: una fila por cada item (refacción o servicio) de cada
    venta del mes, con su ingreso neto, costo y ganancia/pérdida. Sirve para ver qué
    piezas/servicios dejan utilidad y cuáles se vendieron por debajo del costo.

    El ingreso neto por línea se reconstruye respetando los flags de IVA de la venta
    (precio_incluye_iva / iva_exento) y se prorratea el descuento global mediante un
    factor, de modo que la suma de los renglones coincida con el subtotal de la venta.
    El costo sale de costo_unitario_snapshot (congelado al momento de vender).
    """
    try:
        claims = _get_claims(event)
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No autorizado")

        qp = event.get('queryStringParameters') or {}
        year, month = _parse_year_month(qp)
        sucursal_id = qp.get('sucursal_id') or qp.get('sucursalId')
        desde, hasta_excl = _month_range(year, month)

        db = get_tenant_db(tenant_id)

        q_ventas = {
            "createdAt": {"$gte": desde, "$lt": hasta_excl},
            "estado": {"$nin": ["CANCELADA", "ANULADA"]},
        }
        if sucursal_id:
            q_ventas['sucursal_id'] = sucursal_id
        ventas = list(db.ventas.find(q_ventas, {
            'folio': 1, 'cliente_nombre': 1, 'cliente_id': 1, 'items': 1,
            'subtotal': 1, 'descuento': 1, 'orden_id': 1, 'sucursal_id': 1,
            'createdAt': 1, 'estado': 1,
        }))

        renglones = []
        tot_ingreso = 0.0
        tot_costo = 0.0
        grupos = {
            'REFACCION': {'ingreso': 0.0, 'costo': 0.0, 'ganancia': 0.0, 'renglones': 0},
            'SERVICIO':  {'ingreso': 0.0, 'costo': 0.0, 'ganancia': 0.0, 'renglones': 0},
        }
        renglones_con_perdida = 0

        for v in ventas:
            items = v.get('items') or []
            # 1) base (sin IVA) de cada línea respetando flags; acumular para el factor.
            lineas_calc = []
            base_bruta_acum = 0.0
            for it in items:
                try:
                    precio = float(it.get('precio_unitario') or 0)
                    cant = int(it.get('cantidad') or 0)
                except (TypeError, ValueError):
                    precio, cant = 0.0, 0
                producto = it.get('producto') or {}
                incluye_iva = it.get('precio_incluye_iva', producto.get('precio_incluye_iva', True))
                iva_exento = it.get('iva_exento', producto.get('iva_exento', False))
                line_amount = precio * cant
                if iva_exento or not bool(incluye_iva):
                    line_base = line_amount
                else:
                    line_base = line_amount / (1 + IVA_RATE)
                base_bruta_acum += line_base
                lineas_calc.append((it, producto, precio, cant, line_base))

            # 2) factor de descuento: subtotal real / suma de bases brutas.
            subtotal_venta = float(v.get('subtotal') or 0)
            factor = (subtotal_venta / base_bruta_acum) if base_bruta_acum > 0 else 1.0

            fecha_iso = v.get('createdAt')
            if isinstance(fecha_iso, datetime):
                fecha_iso = iso_utc(fecha_iso)

            for it, producto, precio, cant, line_base in lineas_calc:
                ingreso_neto = round(line_base * factor, 2)
                costo_u = float(it.get('costo_unitario_snapshot') or 0)
                costo_ln = round(costo_u * cant, 2)
                ganancia = round(ingreso_neto - costo_ln, 2)

                tipo_raw = (it.get('tipo') or producto.get('tipo') or '').upper()
                tipo = 'SERVICIO' if tipo_raw == 'SERVICIO' else 'REFACCION'

                tot_ingreso += ingreso_neto
                tot_costo += costo_ln
                g = grupos[tipo]
                g['ingreso'] += ingreso_neto
                g['costo'] += costo_ln
                g['ganancia'] += ganancia
                g['renglones'] += 1
                if ganancia < 0:
                    renglones_con_perdida += 1

                renglones.append({
                    'venta_id': str(v.get('_id')),
                    'venta_folio': v.get('folio'),
                    'fecha': fecha_iso,
                    'cliente': v.get('cliente_nombre') or 'Público general',
                    'origen': 'OS' if v.get('orden_id') else 'MOSTRADOR',
                    'tipo': tipo,
                    'nombre': it.get('nombre') or producto.get('nombre') or 'Sin nombre',
                    'no_parte': it.get('no_parte') or producto.get('no_parte') or '',
                    'cantidad': cant,
                    'precio_unitario': round(precio, 2),
                    'ingreso_neto': ingreso_neto,
                    'costo': costo_ln,
                    'ganancia': ganancia,
                    'margen_pct': round((ganancia / ingreso_neto * 100), 2) if ingreso_neto > 0 else 0.0,
                    'es_externo': bool(it.get('es_externo') or producto.get('es_externo')),
                    'proveedor_nombre': it.get('proveedor_nombre') or producto.get('proveedor_nombre') or '',
                })

        renglones.sort(key=lambda r: (r.get('fecha') or '', r.get('venta_folio') or ''), reverse=True)
        ganancia_total = round(tot_ingreso - tot_costo, 2)

        for grp in grupos.values():
            grp['ingreso'] = round(grp['ingreso'], 2)
            grp['costo'] = round(grp['costo'], 2)
            grp['ganancia'] = round(grp['ganancia'], 2)

        return create_response(200, "Concentrado de ventas", {
            'year': year,
            'month': month,
            'sucursal_id': sucursal_id,
            'rango': {"desde": iso_utc(desde), "hasta": iso_utc(hasta_excl)},
            'totales': {
                'ingreso_neto': round(tot_ingreso, 2),
                'costo': round(tot_costo, 2),
                'ganancia': ganancia_total,
                'margen_pct': round((ganancia_total / tot_ingreso * 100), 2) if tot_ingreso > 0 else 0.0,
                'renglones': len(renglones),
                'renglones_con_perdida': renglones_con_perdida,
            },
            'por_tipo': {
                'refacciones': grupos['REFACCION'],
                'servicios': grupos['SERVICIO'],
            },
            'renglones': renglones[:2000],
        })
    except Exception as e:
        return handle_exception(e)


def get_iva_mensual_handler(event, context):
    """GET /contabilidad/iva-mensual?year=&month=&sucursal_id=
    IVA trasladado (ventas) vs IVA acreditable (compras) del mes."""
    try:
        claims = _get_claims(event)
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No autorizado")

        qp = event.get('queryStringParameters') or {}
        year, month = _parse_year_month(qp)
        sucursal_id = qp.get('sucursal_id') or qp.get('sucursalId')
        desde, hasta_excl = _month_range(year, month)

        db = get_tenant_db(tenant_id)

        match_v = {"createdAt": {"$gte": desde, "$lt": hasta_excl},
                   "estado": {"$nin": ["CANCELADA", "ANULADA"]}}
        if sucursal_id:
            match_v['sucursal_id'] = sucursal_id
        agg_v = list(db.ventas.aggregate([
            {"$match": match_v},
            {"$group": {
                "_id": None,
                "subtotal": {"$sum": {"$ifNull": ["$subtotal", 0]}},
                "iva": {"$sum": {"$ifNull": ["$iva", 0]}},
                "total": {"$sum": {"$ifNull": ["$total", 0]}},
                "count": {"$sum": 1},
            }}
        ]))
        v = agg_v[0] if agg_v else {"subtotal": 0, "iva": 0, "total": 0, "count": 0}

        match_c = {"createdAt": {"$gte": desde, "$lt": hasta_excl}, "estado": {"$ne": "CANCELADA"}}
        if sucursal_id:
            match_c['sucursal_id'] = sucursal_id
        agg_c = list(db.compras.aggregate([
            {"$match": match_c},
            {"$group": {
                "_id": None,
                "subtotal": {"$sum": {"$ifNull": ["$subtotal", 0]}},
                "iva": {"$sum": {"$ifNull": ["$iva", 0]}},
                "total": {"$sum": {"$ifNull": ["$total", 0]}},
                "count": {"$sum": 1},
            }}
        ]))
        c = agg_c[0] if agg_c else {"subtotal": 0, "iva": 0, "total": 0, "count": 0}

        iva_cobrado = float(v.get('iva') or 0)
        iva_acreditable = float(c.get('iva') or 0)
        saldo = iva_cobrado - iva_acreditable

        return create_response(200, "IVA mensual", {
            "year": year,
            "month": month,
            "ventas": {
                "subtotal": round(float(v.get('subtotal') or 0), 2),
                "iva": round(iva_cobrado, 2),
                "total": round(float(v.get('total') or 0), 2),
                "count": int(v.get('count') or 0),
            },
            "compras": {
                "subtotal": round(float(c.get('subtotal') or 0), 2),
                "iva": round(iva_acreditable, 2),
                "total": round(float(c.get('total') or 0), 2),
                "count": int(c.get('count') or 0),
            },
            "iva_cobrado": round(iva_cobrado, 2),
            "iva_acreditable": round(iva_acreditable, 2),
            "saldo": round(saldo, 2),
            "tipo_saldo": "POR_PAGAR" if saldo > 0 else ("A_FAVOR" if saldo < 0 else "NEUTRO"),
        })
    except Exception as e:
        return handle_exception(e)
