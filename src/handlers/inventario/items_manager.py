import json
import uuid
from datetime import datetime
from aws_lambda_powertools import Logger
from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.infrastructure.database import get_tenant_db
from bson import ObjectId

logger = Logger()

def list_items_handler(event, context):
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        
        query_params = event.get('queryStringParameters') or {}
        tipo = query_params.get('tipo')
        search = query_params.get('search')
        page = int(query_params.get('page', 1))
        limit = int(query_params.get('limit', 50))
        skip = (page - 1) * limit

        db = get_tenant_db(tenant_id)
        
        # Filtros
        query = {}
        if tipo:
            query['tipo'] = tipo
        if search:
            query['$text'] = {'$search': search}

        total = db.items.count_documents(query)
        items = list(db.items.find(query).skip(skip).limit(limit))
        
        for i in items:
            i['id'] = str(i.pop('_id'))
            
        return create_response(200, "Items obtenidos", {
            "items": items,
            "total": total,
            "page": page,
            "limit": limit
        })
    except Exception as e:
        return handle_exception(e)

def create_item_handler(event, context):
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        
        body = json.loads(event.get('body', '{}'))
        # 1. Validar unicidad de número de parte por tenant
        no_parte = body.get('no_parte') or body.get('noParte')

        if not no_parte:
            return create_response(400, "El número de parte es obligatorio.")
        
        existing_part = db.items.find_one({"no_parte": no_parte})
        if existing_part:
            return create_response(400, f"El número de parte '{no_parte}' ya existe en el inventario.")

        # 2. Asegurar índices (Texto para búsqueda)
        db.items.create_index([("nombre", "text"), ("no_parte", "text")])

        # 3. Preparar item
        tipo = body.get('tipo', 'PRODUCTO')
        nuevo_item = {
            "item_id": str(uuid.uuid4()),
            "tipo": tipo,
            "nombre": body['nombre'],
            "no_parte": no_parte,
            "precio_venta": float(body['precio_venta']),
            "categoria": body.get('categoria'),
            "marca": body.get('marca'),
            "proveedor": body.get('proveedor'),
            "tenant_id": tenant_id,
            "createdAt": datetime.utcnow().isoformat(),
            "activo": body.get('activo', True)
        }

        if tipo == 'PRODUCTO':
            nuevo_item.update({
                "precio_compra": float(body.get('precio_compra', 0)),
                "maneja_inventario": body.get('maneja_inventario', True),
                "stock": int(body.get('stock', 0)),
                "clave_sat": body.get('clave_sat'),
                "unidad_sat": body.get('unidad_sat')
            })
        else: # SERVICIO
            nuevo_item.update({
                "maneja_inventario": False,
                "stock": None
            })

        result = db.items.insert_one(nuevo_item)
        nuevo_item['id'] = str(result.inserted_id)
        del nuevo_item['_id']
        
        return create_response(201, "Item creado exitosamente", nuevo_item)
    except Exception as e:
        return handle_exception(e)

def update_stock_handler(event, context):
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        item_id = event['pathParameters']['id']
        body = json.loads(event.get('body', '{}'))
        
        cantidad = int(body.get('cantidad', 0))
        
        db = get_tenant_db(tenant_id)
        
        # Buscar item y validar que maneja inventario
        item = db.items.find_one({"_id": ObjectId(item_id)})
        if not item:
            return create_response(404, "Item no encontrado.")
        
        if item.get('tipo') == 'SERVICIO' or not item.get('maneja_inventario'):
            return create_response(400, "Este item no maneja inventario.")

        # Actualizar stock con $inc
        result = db.items.find_one_and_update(
            {"_id": ObjectId(item_id)},
            {"$inc": {"stock": cantidad}},
            return_document=True
        )

        result['id'] = str(result.pop('_id'))
        
        return create_response(200, "Stock actualizado", {
            "nuevo_stock": result['stock'],
            "ajuste": cantidad
        })
    except Exception as e:
        return handle_exception(e)
