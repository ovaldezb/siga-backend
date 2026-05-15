import json
from datetime import datetime
from bson import ObjectId
from aws_lambda_powertools import Logger
from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.utils.auth_utils import try_parse_id
from src.shared.infrastructure.database import get_tenant_db

logger = Logger()

@logger.inject_lambda_context
def get_kpis_handler(event, context):
    """GET /reportes/kpis — Obtiene métricas consolidadas (OS + POS)."""
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id: return create_response(403, "No autorizado")

        query_params = event.get('queryStringParameters') or {}
        sucursal_id = query_params.get('sucursal_id')
        
        db = get_tenant_db(tenant_id)
        
        # Filtro base
        filter_base = {"tenant_id": tenant_id}
        if sucursal_id:
            filter_base["sucursal_id"] = sucursal_id

        # 1. INGRESOS HOY (OS + POS)
        today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        
        # Helper to convert createdAt to date if it's a string for match stage
        match_date_expr = {
            "$cond": [
                {"$eq": [{"$type": "$createdAt"}, "string"]},
                {"$dateFromString": {"dateString": "$createdAt"}},
                "$createdAt"
            ]
        }

        # Ventas POS Hoy
        res_ventas_hoy = list(db["ventas"].aggregate([
            {"$match": filter_base},
            {"$addFields": {"__date": match_date_expr}},
            {"$match": {"__date": {"$gte": today}}},
            {"$group": {"_id": None, "total": {"$sum": "$total"}}}
        ]))
        
        # OS Hoy (Entregadas)
        res_os_hoy = list(db["ordenes_servicio"].aggregate([
            {"$match": {**filter_base, "estado": "ENTREGADO"}},
            {"$addFields": {"__date": match_date_expr}},
            {"$match": {"__date": {"$gte": today}}},
            {"$group": {"_id": None, "total": {"$sum": "$total"}}}
        ]))
        
        ventas_hoy = (res_ventas_hoy[0]['total'] if res_ventas_hoy else 0) + \
                     (res_os_hoy[0]['total'] if res_os_hoy else 0)

        # 2. MEJORES CLIENTES (Consolidado: ventas POS + OS ENTREGADO sin venta)
        # Algunos talleres no convierten cada OS en una venta POS (flujo legacy o
        # ENTREGADO directo). Si se agrega sólo desde `ventas`, el ranking queda
        # incompleto. Unimos también desde `ordenes_servicio` con estado ENTREGADO,
        # y consolidamos por cliente_id en Python.
        ventas_por_cliente = list(db["ventas"].aggregate([
            {"$match": filter_base},
            {"$group": {
                "_id": "$cliente_id",
                "total_gastado": {"$sum": "$total"},
                "visitas": {"$sum": 1},
                "nombre_cliente": {"$first": "$cliente_nombre"}
            }}
        ]))

        os_por_cliente = list(db["ordenes_servicio"].aggregate([
            {"$match": {**filter_base, "estado": "ENTREGADO"}},
            {"$group": {
                "_id": "$cliente_snapshot.id",
                "total_gastado": {"$sum": {"$ifNull": ["$total", 0]}},
                "visitas": {"$sum": 1},
                "nombre_cliente": {"$first": {
                    "$concat": [
                        {"$ifNull": ["$cliente_snapshot.nombre", ""]},
                        " ",
                        {"$ifNull": ["$cliente_snapshot.apellido_paterno", ""]}
                    ]
                }}
            }}
        ]))

        consolidado = {}
        for row in ventas_por_cliente + os_por_cliente:
            cid = row.get("_id")
            if not cid:
                continue
            entry = consolidado.setdefault(cid, {
                "_id": cid,
                "total_gastado": 0,
                "visitas": 0,
                "nombre_cliente": None,
            })
            entry["total_gastado"] += row.get("total_gastado") or 0
            entry["visitas"] += row.get("visitas") or 0
            if not entry["nombre_cliente"]:
                nombre = (row.get("nombre_cliente") or "").strip()
                if nombre:
                    entry["nombre_cliente"] = nombre

        top_clientes = sorted(
            consolidado.values(),
            key=lambda x: x["total_gastado"],
            reverse=True,
        )[:5]

        # 3. RENDIMIENTO MECÁNICOS (Solo OS)
        # ticket_promedio se calcula solo sobre OS ENTREGADAS. Promediar sobre OS en
        # COTIZADO / EN_PROCESO / CANCELADO distorsiona la métrica (incluye totales
        # parciales o ceros). Las cuentas de "completadas" y "en_proceso" siguen
        # contando todos los estados relevantes para la barra de rendimiento.
        mecanicos_stats = list(db["ordenes_servicio"].aggregate([
            {"$match": filter_base},
            {"$group": {
                "_id": "$mecanico_id",
                "nombre": {"$first": "$mecanico_nombre"},
                "completadas": {"$sum": {"$cond": [{"$eq": ["$estado", "ENTREGADO"]}, 1, 0]}},
                "en_proceso": {"$sum": {"$cond": [{"$eq": ["$estado", "EN_PROCESO"]}, 1, 0]}},
                "_total_entregado_sum": {"$sum": {"$cond": [{"$eq": ["$estado", "ENTREGADO"]}, {"$ifNull": ["$total", 0]}, 0]}},
                "_entregadas_count": {"$sum": {"$cond": [{"$eq": ["$estado", "ENTREGADO"]}, 1, 0]}}
            }},
            {"$addFields": {
                "ticket_promedio": {
                    "$cond": [
                        {"$gt": ["$_entregadas_count", 0]},
                        {"$divide": ["$_total_entregado_sum", "$_entregadas_count"]},
                        0
                    ]
                }
            }},
            {"$project": {"_total_entregado_sum": 0, "_entregadas_count": 0}},
            {"$sort": {"completadas": -1}}
        ]))

        # 4. TENDENCIAS (History - 6 meses)
        # Restamos meses calendario exactos. Antes se usaba timedelta(days=i*30),
        # que con meses de 28/31 días duplicaba o se saltaba meses en el reporte.
        history = []
        now = datetime.utcnow()
        for i in range(5, -1, -1):
            month_index = now.year * 12 + (now.month - 1) - i
            year = month_index // 12
            month = month_index % 12 + 1
            month_year = f"{year:04d}-{month:02d}"
            history.append({"mes": month_year, "total": 0, "count": 0})

        # Helper to convert createdAt to date if it's a string
        date_expr = {
            "$cond": [
                {"$eq": [{"$type": "$createdAt"}, "string"]},
                {"$dateFromString": {"dateString": "$createdAt"}},
                "$createdAt"
            ]
        }

        # Agregación mensual de Ventas POS
        ventas_mensuales = list(db["ventas"].aggregate([
            {"$match": filter_base},
            {"$addFields": {"__date": date_expr}},
            {"$group": {
                "_id": {"$dateToString": {"format": "%Y-%m", "date": "$__date"}},
                "total": {"$sum": "$total"},
                "count": {"$sum": 1}
            }}
        ]))
        
        # Agregación mensual de OS
        os_mensuales = list(db["ordenes_servicio"].aggregate([
            {"$match": {**filter_base, "estado": "ENTREGADO"}},
            {"$addFields": {"__date": date_expr}},
            {"$group": {
                "_id": {"$dateToString": {"format": "%Y-%m", "date": "$__date"}},
                "total": {"$sum": "$total"},
                "count": {"$sum": 1}
            }}
        ]))

        # Combinar resultados en history
        for res in (ventas_mensuales + os_mensuales):
            if not res.get('_id'): continue
            for h in history:
                if h['mes'] == res['_id']:
                    h['total'] += res['total']
                    h['count'] += res['count']

        # 5. UTILIDAD Y MARGEN (Consolidado)
        # Utility from Sales (POS)
        res_util_ventas = list(db["ventas"].aggregate([
            {"$match": filter_base},
            {"$unwind": "$items"},
            {"$group": {
                "_id": None,
                "utilidad": {"$sum": {"$multiply": [{"$subtract": ["$items.precio_unitario", {"$ifNull": ["$items.producto.precio_compra", 0]}]}, "$items.cantidad"]}}
            }}
        ]))
        
        # Utility from OS
        # Las cortesías (no_cobrar) no generan ingreso: su precioVenta cuenta como 0,
        # de modo que su costo (precioCompra) reste correctamente a la utilidad del taller.
        res_util_os = list(db["ordenes_servicio"].aggregate([
            {"$match": {**filter_base, "estado": "ENTREGADO"}},
            {"$unwind": {"path": "$puntosArreglar", "preserveNullAndEmptyArrays": True}},
            {"$unwind": {"path": "$puntosArreglar.items", "preserveNullAndEmptyArrays": True}},
            {"$group": {
                "_id": None,
                "utilidad": {"$sum": {"$multiply": [
                    {"$subtract": [
                        {"$cond": [
                            {"$eq": [{"$ifNull": ["$puntosArreglar.items.no_cobrar", False]}, True]},
                            0,
                            {"$ifNull": ["$puntosArreglar.items.precioVenta", 0]}
                        ]},
                        {"$ifNull": ["$puntosArreglar.items.precioCompra", 0]}
                    ]},
                    {"$ifNull": ["$puntosArreglar.items.piezas", 0]}
                ]}}
            }}
        ]))
        
        utilidad_total = (res_util_ventas[0]['utilidad'] if res_util_ventas else 0) + \
                         (res_util_os[0]['utilidad'] if res_util_os else 0)
        
        # Ingresos totales para margen
        ingresos_totales = list(db["ventas"].aggregate([
            {"$match": filter_base},
            {"$group": {"_id": None, "total": {"$sum": "$total"}}}
        ]))
        ingresos_totales_val = (ingresos_totales[0]['total'] if ingresos_totales else 0)
        margen = (utilidad_total / ingresos_totales_val * 100) if ingresos_totales_val > 0 else 0

        # 6. CITAS PENDIENTES
        citas_pendientes = db["citas"].count_documents({**filter_base, "estado": "pendiente"})

        # 6.5 CONTEOS GLOBALES PARA DASHBOARD
        clientes_filter = {}
        if sucursal_id:
            clientes_filter["sucursal_id"] = sucursal_id
        total_clientes = db["clientes"].count_documents(clientes_filter)
        ordenes_activas = db["ordenes_servicio"].count_documents({
            **filter_base,
            "estado": {"$in": ["RECEPCION", "COTIZADO", "APROBADO", "EN_PROCESO", "FINALIZADO"]}
        })
        cuentas_por_cobrar_agg = list(db["ventas"].aggregate([
            {"$match": {**filter_base, "saldo_pendiente": {"$gt": 0}}},
            {"$group": {"_id": None, "total": {"$sum": "$saldo_pendiente"}, "count": {"$sum": 1}}}
        ]))
        cxc_total = cuentas_por_cobrar_agg[0]['total'] if cuentas_por_cobrar_agg else 0
        cxc_count = cuentas_por_cobrar_agg[0]['count'] if cuentas_por_cobrar_agg else 0

        # 7. PRODUCTO MÁS VENDIDO (Top 1)
        top_producto = list(db["ventas"].aggregate([
            {"$match": filter_base},
            {"$unwind": "$items"},
            {"$group": {
                "_id": {"$ifNull": ["$items.producto.id", "$items.id"]},
                "nombre": {"$first": "$items.nombre"},
                "cantidad": {"$sum": "$items.cantidad"}
            }},
            {"$sort": {"cantidad": -1}},
            {"$limit": 1}
        ]))

        return create_response(200, "KPIs consolidados generados", {
            "top_clientes": top_clientes,
            "mecanicos": mecanicos_stats,
            "history": history,
            "ventas_hoy": ventas_hoy,
            "citas_pendientes": citas_pendientes,
            "top_producto": top_producto[0] if top_producto else None,
            "utilidad_total": round(utilidad_total, 2),
            "margen_promedio": round(margen, 2),
            "total_clientes": total_clientes,
            "ordenes_activas": ordenes_activas,
            "cuentas_por_cobrar": {
                "total": round(cxc_total, 2),
                "count": cxc_count
            }
        })

    except Exception as e:
        logger.exception("Error in get_kpis_handler")
        return handle_exception(e)


@logger.inject_lambda_context
def get_customer_history_handler(event, context):
    """GET /reportes/cliente/{id} — Historial completo de un cliente (360°)."""
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id: return create_response(403, "No autorizado")

        cliente_id = event['pathParameters']['id']
        db = get_tenant_db(tenant_id)

        # 1. Citas
        citas = list(db["citas"].find({"cliente_id": cliente_id}).sort("fecha", -1))
        for c in citas: 
            c["id"] = str(c.pop("_id"))

        # 2. Ordenes de Servicio
        # Buscamos por cliente_snapshot.id (que es como se guarda) o por cliente_id si existiera
        ordenes = list(db["ordenes_servicio"].find({
            "$or": [
                {"cliente_snapshot.id": cliente_id},
                {"cliente_id": cliente_id}
            ]
        }).sort("createdAt", -1))
        for o in ordenes:
            o["id"] = str(o.pop("_id"))
            if "createdAt" in o and isinstance(o["createdAt"], datetime):
                o["createdAt"] = o["createdAt"].isoformat()

        # 3. Ventas (POS + OS)
        ventas = list(db["ventas"].find({"cliente_id": cliente_id}).sort("createdAt", -1))
        total_gastado = 0
        items_comprados = {}

        for v in ventas:
            v["id"] = str(v.pop("_id"))
            total_gastado += v.get("total", 0)
            if "createdAt" in v and isinstance(v["createdAt"], datetime):
                v["createdAt"] = v["createdAt"].isoformat()
            
            # Analizar qué compra el cliente
            for item in v.get("items", []):
                prod = item.get("producto", {})
                name = prod.get("nombre") or item.get("nombre")
                if name:
                    items_comprados[name] = items_comprados.get(name, 0) + item.get("cantidad", 1)

        # 4. Formatear items comprados
        resumen_compras = sorted(
            [{"nombre": k, "cantidad": v} for k, v in items_comprados.items()],
            key=lambda x: x["cantidad"],
            reverse=True
        )

        return create_response(200, "Historial del cliente recuperado", {
            "citas": citas,
            "ordenes": ordenes,
            "ventas": ventas,
            "metricas": {
                "total_gastado": round(total_gastado, 2),
                "total_visitas": len(ventas),
                "resumen_compras": resumen_compras
            }
        })

    except Exception as e:
        logger.exception("Error in get_customer_history_handler")
        return handle_exception(e)
