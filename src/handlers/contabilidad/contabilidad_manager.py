"""Reportes contables: inventario valuado, envejecimiento de CxC/CxP, margen de ventas."""

from datetime import datetime, timedelta
from aws_lambda_powertools import Logger

from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.infrastructure.database import get_tenant_db

logger = Logger()


def _get_claims(event):
    return event.get('requestContext', {}).get('authorizer', {}).get('claims', {}) or {}


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
                'fecha': v.get('createdAt').isoformat() + "Z" if isinstance(v.get('createdAt'), datetime) else v.get('createdAt'),
            })

        margen_total = ingresos - costos
        margen_pct_total = (margen_total / ingresos * 100) if ingresos > 0 else 0

        return create_response(200, "Margen de ventas", {
            "rango": {"desde": desde.isoformat(), "hasta": hasta.isoformat()},
            "ingresos_netos": round(ingresos, 2),
            "costo_ventas": round(costos, 2),
            "margen_bruto": round(margen_total, 2),
            "margen_pct": round(margen_pct_total, 2),
            "ventas": len(ventas),
            "detalle": detalle[:200],
        })
    except Exception as e:
        return handle_exception(e)
