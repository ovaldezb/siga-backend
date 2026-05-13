import json
from datetime import datetime, timedelta
from bson import ObjectId
from aws_lambda_powertools import Logger
from src.shared.utils.response_handler import create_response, handle_exception
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

        # 2. MEJORES CLIENTES (Consolidado)
        # Nota: En un sistema real usaríamos una colección de clientes, 
        # pero aquí promediamos desde ventas y OS por simplicidad del reporte actual
        top_clientes = list(db["ventas"].aggregate([
            {"$match": filter_base},
            {"$group": {
                "_id": "$cliente_id",
                "total_gastado": {"$sum": "$total"},
                "visitas": {"$sum": 1},
                "nombre_cliente": {"$first": "$cliente_nombre"} # Si se guarda en venta
            }},
            {"$sort": {"total_gastado": -1}},
            {"$limit": 5}
        ]))

        # 3. RENDIMIENTO MECÁNICOS (Solo OS)
        mecanicos_stats = list(db["ordenes_servicio"].aggregate([
            {"$match": filter_base},
            {"$group": {
                "_id": "$mecanico_id",
                "nombre": {"$first": "$mecanico_nombre"},
                "completadas": {"$sum": {"$cond": [{"$eq": ["$estado", "ENTREGADO"]}, 1, 0]}},
                "en_proceso": {"$sum": {"$cond": [{"$eq": ["$estado", "EN_PROCESO"]}, 1, 0]}},
                "ticket_promedio": {"$avg": "$total"}
            }},
            {"$sort": {"completadas": -1}}
        ]))

        # 4. TENDENCIAS (History - 6 meses)
        history = []
        for i in range(5, -1, -1):
            date = datetime.now() - timedelta(days=i*30)
            month_year = date.strftime("%Y-%m")
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

        # 5. CITAS PENDIENTES
        citas_pendientes = db["citas"].count_documents({**filter_base, "estado": "pendiente"})

        # 6. PRODUCTO MÁS VENDIDO (Top 1)
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
            "top_producto": top_producto[0] if top_producto else None
        })

    except Exception as e:
        logger.exception("Error in get_kpis_handler")
        return handle_exception(e)
