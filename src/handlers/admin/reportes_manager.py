import json
from datetime import datetime, timedelta
from bson import ObjectId
from aws_lambda_powertools import Logger
from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.infrastructure.database import get_tenant_db

logger = Logger()

@logger.inject_lambda_context
def get_kpis_handler(event, context):
    """GET /reportes/kpis — Obtiene métricas generales y tendencias."""
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id: return create_response(403, "No autorizado")

        query_params = event.get('queryStringParameters') or {}
        sucursal_id = query_params.get('sucursal_id')
        
        # Filtro base para sucursal (estricto para aislamiento)
        sucursal_filter = {}
        if sucursal_id:
            sucursal_filter = {"sucursal_id": sucursal_id}

        db = get_tenant_db(tenant_id)
        
        # 1. MEJORES CLIENTES
        match_clientes = {"tenant_id": tenant_id, "estado": {"$in": ["FINALIZADO", "ENTREGADO"]}}
        if sucursal_filter: match_clientes.update(sucursal_filter)
        
        top_clientes = list(db["ordenes_servicio"].aggregate([
            {"$match": match_clientes},
            {"$group": {
                "_id": "$cliente_id",
                "total_gastado": {"$sum": "$total"},
                "visitas": {"$sum": 1},
                "nombre_cliente": {"$first": "$cliente_nombre"}
            }},
            {"$sort": {"total_gastado": -1}},
            {"$limit": 5}
        ]))

        # 2. PROVEEDORES
        match_proveedores = {"tenant_id": tenant_id, "tipo": "PRODUCTO"}
        if sucursal_filter: match_proveedores.update(sucursal_filter)
        
        top_proveedores = list(db["items"].aggregate([
            {"$match": match_proveedores},
            {"$group": {
                "_id": "$proveedor",
                "total_inventario": {"$sum": {"$multiply": ["$stock", "$precio_compra"]}},
                "productos": {"$sum": 1}
            }},
            {"$sort": {"total_inventario": -1}},
            {"$limit": 5}
        ]))

        # 3. RENDIMIENTO MECÁNICOS
        match_mecanicos = {"tenant_id": tenant_id, "mecanico_id": {"$ne": None}}
        if sucursal_filter: match_mecanicos.update(sucursal_filter)
        
        mecanicos_stats = list(db["ordenes_servicio"].aggregate([
            {"$match": match_mecanicos},
            {"$group": {
                "_id": "$mecanico_id",
                "nombre": {"$first": "$mecanico_nombre"},
                "completadas": {"$sum": {"$cond": [{"$eq": ["$estado", "ENTREGADO"]}, 1, 0]}},
                "en_proceso": {"$sum": {"$cond": [{"$eq": ["$estado", "EN_PROCESO"]}, 1, 0]}},
                "ticket_promedio": {"$avg": "$total"}
            }},
            {"$sort": {"completadas": -1}}
        ]))

        # 4. TRENDS
        history = []
        for i in range(5, -1, -1):
            date = datetime.now() - timedelta(days=i*30)
            month_year = date.strftime("%Y-%m")
            history.append({"mes": month_year, "total": 0, "count": 0})

        match_ingresos = {"tenant_id": tenant_id, "estado": {"$in": ["FINALIZADO", "ENTREGADO"]}}
        if sucursal_filter: match_ingresos.update(sucursal_filter)
        
        ingresos_mensuales = list(db["ordenes_servicio"].aggregate([
            {"$match": match_ingresos},
            {"$group": {
                "_id": {"$dateToString": {"format": "%Y-%m", "date": "$createdAt"}},
                "total": {"$sum": "$total"},
                "count": {"$sum": 1}
            }}
        ]))

        for res in ingresos_mensuales:
            for h in history:
                if h['mes'] == res['_id']:
                    h['total'] = res['total']
                    h['count'] = res['count']

        # 5. VENTAS HOY
        today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        match_hoy = {
            "tenant_id": tenant_id, 
            "createdAt": {"$gte": today}, 
            "estado": {"$in": ["FINALIZADO", "ENTREGADO"]}
        }
        if sucursal_filter: match_hoy.update(sucursal_filter)
        
        res_hoy = list(db["ordenes_servicio"].aggregate([
            {"$match": match_hoy},
            {"$group": {"_id": None, "total": {"$sum": "$total"}}}
        ]))
        ventas_hoy = res_hoy[0]['total'] if res_hoy else 0

        # 6. CITAS PENDIENTES
        match_citas = {"tenant_id": tenant_id, "estado": "pendiente"}
        if sucursal_filter: match_citas.update(sucursal_filter)
        citas_pendientes = db["citas"].count_documents(match_citas)

        return create_response(200, "KPIs generados", {
            "top_clientes": top_clientes,
            "top_proveedores": top_proveedores,
            "mecanicos": mecanicos_stats,
            "history": history,
            "ventas_hoy": ventas_hoy,
            "citas_pendientes": citas_pendientes
        })

    except Exception as e:
        return handle_exception(e)
