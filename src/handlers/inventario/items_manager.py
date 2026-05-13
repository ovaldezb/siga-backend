import json
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
        sucursal_id = query_params.get('sucursalId') or query_params.get('sucursal_id')
        page = int(query_params.get('page', 1))
        limit = int(query_params.get('limit', 50))
        skip = (page - 1) * limit

        db = get_tenant_db(tenant_id)
        
        # Filtros
        query = {}
        and_conditions = []
        if sucursal_id:
            and_conditions.append({'sucursal_id': sucursal_id})
            
        if tipo:
            query['tipo'] = tipo
            
        if search:
            and_conditions.append({'$or': [
                {"nombre": {"$regex": search, "$options": "i"}},
                {"no_parte": {"$regex": search, "$options": "i"}}
            ]})
            
        if and_conditions:
            query['$and'] = and_conditions

        total = db["items"].count_documents(query)
        items_result = list(db["items"].find(query).skip(skip).limit(limit))
        
        for i in items_result:
            i['id'] = str(i.pop('_id'))
            if 'sucursal_id' in i:
                i['sucursalId'] = i.pop('sucursal_id')
            
        return create_response(200, "Items obtenidos", {
            "items": items_result,
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
        # 1. Validar campos obligatorios
        nombre = body.get('nombre')
        precio_venta = body.get('precio_venta')
        no_parte = body.get('no_parte') or body.get('noParte')

        if not nombre or not no_parte or precio_venta is None:
            return create_response(400, "Nombre, No. Parte y Precio Venta son obligatorios.")
        
        db = get_tenant_db(tenant_id)
        
        # 2. Validar duplicados
        existing_part = db["items"].find_one({"no_parte": no_parte})
        if existing_part:
            return create_response(400, f"El número de parte '{no_parte}' ya existe en el inventario.")

        # 3. Asegurar índices (Texto para búsqueda) - Con try para no bloquear
        try:
            db["items"].create_index([("nombre", "text"), ("no_parte", "text")])
        except Exception as e:
            logger.warning(f"No se pudo crear/verificar el índice de texto: {str(e)}")

        # 4. Funciones auxiliares de conversión segura
        def to_float(val):
            try:
                return float(val) if val not in [None, ""] else 0.0
            except (ValueError, TypeError):
                return 0.0

        def to_int(val):
            try:
                return int(val) if val not in [None, ""] else 0
            except (ValueError, TypeError):
                return 0

        # 5. Preparar item
        tipo = body.get('tipo', 'PRODUCTO')
        nuevo_item = {
            "tipo": tipo,
            "nombre": body['nombre'],
            "no_parte": body['no_parte'] or body.get('noParte'),
            "precio_venta": to_float(body['precio_venta']),
            "precio_taller": to_float(body.get('precio_taller', 0)),
            "precio_cliente": to_float(body.get('precio_cliente', 0)),
            "precio_distribuidor": to_float(body.get('precio_distribuidor', 0)),
            "precio_compra": to_float(body.get('precio_compra', 0)),
            "categoria": body.get('categoria', 'GENERAL'),
            "marca": body.get('marca', ''),
            "proveedor": body.get('proveedor', ''),
            "sucursal_id": body.get('sucursalId') or body.get('sucursal_id'),
            "tenant_id": tenant_id,
            "createdAt": datetime.utcnow().isoformat() + "Z",
            "activo": body.get('activo', True),
            "icon": body.get('icon', 'ri-archive-line')
        }

        if tipo == 'PRODUCTO':
            nuevo_item.update({
                "precio_compra": to_float(body.get('precio_compra')),
                "maneja_inventario": body.get('maneja_inventario', True),
                "stock": to_int(body.get('stock')),
                "clave_sat": body.get('clave_sat'),
                "unidad_sat": body.get('unidad_sat')
            })
        else: # SERVICIO
            nuevo_item.update({
                "maneja_inventario": False,
                "stock": None
            })

        result = db["items"].insert_one(nuevo_item)
        nuevo_item['id'] = str(result.inserted_id)
        del nuevo_item['_id']
        if 'sucursal_id' in nuevo_item:
            nuevo_item['sucursalId'] = nuevo_item.pop('sucursal_id')
        
        return create_response(201, "Item creado exitosamente", nuevo_item)
    except Exception as e:
        return handle_exception(e)

def get_item_handler(event, context):
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        item_id = event['pathParameters']['id']
        db = get_tenant_db(tenant_id)
        item = db["items"].find_one({"_id": ObjectId(item_id)})

        if not item:
            return create_response(404, "Item no encontrado.")

        item['id'] = str(item.pop('_id'))
        if 'sucursal_id' in item:
            item['sucursalId'] = item.pop('sucursal_id')
        return create_response(200, "Detalle del item", item)
    except Exception as e:
        return handle_exception(e)


def update_item_handler(event, context):
    """PUT /items/{id} — Actualiza datos del item (no incluye stock)."""
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        item_id = event['pathParameters']['id']
        body = json.loads(event.get('body', '{}'))

        allowed = {
            "nombre", "no_parte", "tipo", "precio_venta", 
            "precio_taller", "precio_cliente", "precio_distribuidor",
            "precio_compra", "categoria", "marca", "proveedor", 
            "clave_sat", "unidad_sat", "maneja_inventario", "activo", "icon",
            "sucursal_id"
        }
        
        # Mapear sucursalId a sucursal_id
        if 'sucursalId' in body:
            body['sucursal_id'] = body.pop('sucursalId')

        update_data = {k: body[k] for k in allowed if k in body}

        if not update_data:
            return create_response(400, "No hay campos válidos para actualizar.")

        update_data['updatedAt'] = datetime.utcnow().isoformat() + "Z"

        db = get_tenant_db(tenant_id)
        result = db["items"].update_one(
            {"_id": ObjectId(item_id)},
            {"$set": update_data}
        )

        if result.matched_count == 0:
            return create_response(404, "Item no encontrado.")

        item = db["items"].find_one({"_id": ObjectId(item_id)})
        item['id'] = str(item.pop('_id'))
        if 'sucursal_id' in item:
            item['sucursalId'] = item.pop('sucursal_id')
        return create_response(200, "Item actualizado", item)
    except Exception as e:
        return handle_exception(e)


def delete_item_handler(event, context):
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        item_id = event['pathParameters']['id']
        db = get_tenant_db(tenant_id)
        
        # 1. Verificar stock antes de borrar
        item = db["items"].find_one({"_id": ObjectId(item_id)})
        if not item:
            return create_response(404, "Item no encontrado.")
            
        if item.get('tipo') == 'PRODUCTO' and item.get('stock', 0) > 0:
            return create_response(400, f"No se puede eliminar un producto con stock activo ({item['stock']}). Por favor ajuste el stock a 0 primero.")

        # 2. Proceder con el borrado
        result = db["items"].delete_one({"_id": ObjectId(item_id)})

        if result.deleted_count == 0:
            return create_response(404, "Item no encontrado.")

        return create_response(200, "Item eliminado")
    except Exception as e:
        return handle_exception(e)


def update_stock_handler(event, context):
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        item_id = event['pathParameters']['id']
        body = json.loads(event.get('body', '{}'))

        cantidad = int(body.get('cantidad', 0))

        sucursal_id = body.get('sucursalId') or body.get('sucursal_id')
        
        db = get_tenant_db(tenant_id)
        
        # Buscar item y validar que maneja inventario y sucursal
        query = {"_id": ObjectId(item_id)}
        if sucursal_id:
            query["sucursal_id"] = sucursal_id
            
        item = db["items"].find_one(query)
        if not item:
            return create_response(404, "Item no encontrado.")
        
        if item.get('tipo') == 'SERVICIO' or not item.get('maneja_inventario'):
            return create_response(400, "Este item no maneja inventario.")

        result = db["items"].find_one_and_update(
            query,
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
