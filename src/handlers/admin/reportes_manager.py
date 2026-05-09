import json
from datetime import datetime, timedelta
from bson import ObjectId
from src.utils.response import create_response
from src.utils.db import get_tenant_db
from src.utils.logger import logger

def get_kpis_handler(event, context):
    """GET /reportes/kpis — Obtiene métricas generales y tendencias."""
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id: return create_response(403, "No autorizado")

        db = get_tenant_db(tenant_id)
        
        # 1. MEJORES CLIENTES (Top 5 por monto en órdenes finalizadas/entregadas)
        top_clientes = list(db["ordenes"].aggregate([
            {"$match": {"tenant_id": tenant_id, "estado": {"$in": ["FINALIZADO", "ENTREGADO"]}}},
            {"$group": {
                "_id": "$cliente_id",
                "total_gastado": {"$sum": "$total"},
                "visitas": {"$sum": 1},
                "nombre_cliente": {"$first": "$cliente_nombre"}
            }},
            {"$sort": {"total_gastado": -1}},
            {"$limit": 5}
        ]))

        # 2. PROVEEDORES (Gasto por proveedor en items)
        # Nota: Aquí asumo que las órdenes tienen items con proveedor
        top_proveedores = list(db["items"].aggregate([
            {"$match": {"tenant_id": tenant_id, "tipo": "PRODUCTO"}},
            {"$group": {
                "_id": "$proveedor",
                "total_inventario": {"$sum": {"$multiply": ["$stock", "$precio_compra"]}},
                "productos": {"$sum": 1}
            }},
            {"$sort": {"total_inventario": -1}},
            {"$limit": 5}
        ]))

        # 3. RENDIMIENTO MECÁNICOS (Órdenes por mecánico)
        mecanicos_stats = list(db["ordenes"].aggregate([
            {"$match": {"tenant_id": tenant_id, "mecanico_id": {"$ne": None}}},
            {"$group": {
                "_id": "$mecanico_id",
                "nombre": {"$first": "$mecanico_nombre"},
                "completadas": {"$sum": {"$cond": [{"$eq": ["$estado", "ENTREGADO"]}, 1, 0]}},
                "en_proceso": {"$sum": {"$cond": [{"$eq": ["$estado", "EN_PROCESO"]}, 1, 0]}},
                "ticket_promedio": {"$avg": "$total"}
            }},
            {"$sort": {"completadas": -1}}
        ]))

        # 4. TRENDS (Ingresos últimos 6 meses)
        # Generar labels de meses
        history = []
        for i in range(5, -1, -1):
            date = datetime.now() - timedelta(days=i*30)
            month_year = date.strftime("%Y-%m")
            history.append({"mes": month_year, "total": 0})

        # Agregación por mes
        ingresos_mensuales = list(db["ordenes"].aggregate([
            {"$match": {"tenant_id": tenant_id, "estado": {"$in": ["FINALIZADO", "ENTREGADO"]}}},
            {"$group": {
                "_id": {"$substr": ["$createdAt", 0, 7]},
                "total": {"$sum": "$total"}
            }}
        ]))

        for res in ingresos_mensuales:
            for h in history:
                if h['mes'] == res['_id']:
                    h['total'] = res['total']

        return create_response(200, "KPIs generados", {
            "top_clientes": top_clientes,
            "top_proveedores": top_proveedores,
            "mecanicos": mecanicos_stats,
            "history": history
        })

    except Exception as e:
        logger.exception("Error en get_kpis_handler")
        return create_response(500, str(e))
