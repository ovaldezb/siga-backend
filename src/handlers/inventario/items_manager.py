import json
from datetime import datetime
from aws_lambda_powertools import Logger
from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.infrastructure.database import get_tenant_db
from src.shared.utils.auth_utils import try_parse_id, resolve_sucursal_scope, is_admin
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

        # Resolver scope de sucursal: no-admin se filtra automáticamente a sus sucursales
        scope_list, scope_err = resolve_sucursal_scope(claims, db, sucursal_id)
        if scope_err:
            return create_response(403, scope_err)

        # Filtros
        query = {}
        and_conditions = []
        if scope_list is not None:
            if len(scope_list) == 1:
                and_conditions.append({'sucursal_id': scope_list[0]})
            else:
                and_conditions.append({'sucursal_id': {'$in': scope_list}})
            
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

        sucursal_nueva = body.get('sucursalId') or body.get('sucursal_id')

        # 2. Validar duplicados scoped por sucursal — el modelo multi-sucursal permite
        #    el mismo SKU en sucursales distintas (stock por sucursal). Sólo bloquear
        #    si ya existe en esta misma sucursal.
        dup_query = {"no_parte": no_parte}
        if sucursal_nueva:
            dup_query["sucursal_id"] = sucursal_nueva
        existing_part = db["items"].find_one(dup_query)
        if existing_part:
            return create_response(400, f"El número de parte '{no_parte}' ya existe en esta sucursal.")

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
            # Flags fiscales: por defecto los precios capturados YA incluyen IVA (convención SIGA)
            "precio_incluye_iva": bool(body.get('precio_incluye_iva', True)),
            "iva_exento": bool(body.get('iva_exento', False)),
            "categoria": body.get('categoria', 'GENERAL'),
            "marca": body.get('marca', ''),
            "proveedor": body.get('proveedor', ''),
            "proveedor_id": body.get('proveedor_id') or body.get('proveedorId'),
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
        query_params = event.get('queryStringParameters') or {}
        sucursal_id = query_params.get('sucursalId') or query_params.get('sucursal_id')

        db = get_tenant_db(tenant_id)
        query = {"_id": ObjectId(item_id)}

        # SUPER_ADMIN/ADMIN con ?ignoreScope=1 bypass scope (cross-sucursal lookup)
        ignore_scope = (query_params.get('ignoreScope') == '1') and is_admin(claims)
        if not ignore_scope:
            scope_list, scope_err = resolve_sucursal_scope(claims, db, sucursal_id)
            if scope_err:
                return create_response(403, scope_err)
            if scope_list is not None:
                # Filtrar por una o varias sucursales permitidas
                query["sucursal_id"] = scope_list[0] if len(scope_list) == 1 else {"$in": scope_list}

        item = db["items"].find_one(query)

        if not item:
            return create_response(404, "Item no encontrado.")

        item['id'] = str(item.pop('_id'))
        if 'sucursal_id' in item:
            item['sucursalId'] = item.pop('sucursal_id')
        return create_response(200, "Detalle del item", item)
    except Exception as e:
        return handle_exception(e)


def update_item_handler(event, context):
    """PUT /items/{id} — Actualiza datos del item (no incluye stock). Scoped por sucursal activa."""
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        item_id = event['pathParameters']['id']
        body = json.loads(event.get('body', '{}'))

        # Sucursal scope: usamos el sucursalId del body o query como guard salvo ADMIN+ignoreScope
        query_params = event.get('queryStringParameters') or {}
        sucursal_id_guard = (
            body.get('sucursalId') or body.get('sucursal_id')
            or query_params.get('sucursalId') or query_params.get('sucursal_id')
        )
        ignore_scope = (query_params.get('ignoreScope') == '1') and is_admin(claims)

        db = get_tenant_db(tenant_id)

        # Enforce scope contra las sucursales permitidas del usuario, salvo bypass de admin
        if not ignore_scope:
            scope_list, scope_err = resolve_sucursal_scope(claims, db, sucursal_id_guard)
            if scope_err:
                return create_response(403, scope_err)
            if scope_list is not None and len(scope_list) == 1:
                # No-admin con sucursal específica: usar como guard
                sucursal_id_guard = scope_list[0]
            elif scope_list is not None and len(scope_list) > 1 and not sucursal_id_guard:
                # No-admin sin sucursal explícita pero con múltiples permitidas: no permitir update ciego
                return create_response(400, "Especifique 'sucursal_id' para actualizar el item.")

        allowed = {
            "nombre", "no_parte", "tipo", "precio_venta",
            "precio_taller", "precio_cliente", "precio_distribuidor",
            "precio_compra", "precio_incluye_iva", "iva_exento",
            "categoria", "marca", "proveedor", "proveedor_id",
            "clave_sat", "unidad_sat", "maneja_inventario", "activo", "icon",
            "sucursal_id"
        }

        # Mapear IDs
        if 'sucursalId' in body:
            body['sucursal_id'] = body.pop('sucursalId')
        if 'proveedorId' in body:
            body['proveedor_id'] = body.pop('proveedorId')

        update_data = {k: body[k] for k in allowed if k in body}

        if not update_data:
            return create_response(400, "No hay campos válidos para actualizar.")

        update_data['updatedAt'] = datetime.utcnow().isoformat() + "Z"

        update_query = {"_id": ObjectId(item_id)}
        if sucursal_id_guard and not ignore_scope:
            update_query["sucursal_id"] = sucursal_id_guard
        result = db["items"].update_one(update_query, {"$set": update_data})

        if result.matched_count == 0:
            return create_response(404, "Item no encontrado en esta sucursal.")

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
        query_params = event.get('queryStringParameters') or {}
        sucursal_id_guard = query_params.get('sucursalId') or query_params.get('sucursal_id')
        ignore_scope = (query_params.get('ignoreScope') == '1') and is_admin(claims)

        db = get_tenant_db(tenant_id)

        # Enforce scope contra las sucursales permitidas del usuario
        if not ignore_scope:
            scope_list, scope_err = resolve_sucursal_scope(claims, db, sucursal_id_guard)
            if scope_err:
                return create_response(403, scope_err)
            if scope_list is not None and len(scope_list) == 1:
                sucursal_id_guard = scope_list[0]
            elif scope_list is not None and len(scope_list) > 1 and not sucursal_id_guard:
                return create_response(400, "Especifique 'sucursal_id' para eliminar el item.")

        # 1. Verificar stock antes de borrar (scoped)
        find_query = {"_id": ObjectId(item_id)}
        if sucursal_id_guard and not ignore_scope:
            find_query["sucursal_id"] = sucursal_id_guard

        item = db["items"].find_one(find_query)
        if not item:
            return create_response(404, "Item no encontrado en esta sucursal.")

        if item.get('tipo') == 'PRODUCTO' and item.get('stock', 0) > 0:
            return create_response(400, f"No se puede eliminar un producto con stock activo ({item['stock']}). Por favor ajuste el stock a 0 primero.")

        # 2. Proceder con el borrado (sólo el doc scoped)
        result = db["items"].delete_one(find_query)

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

        try:
            cantidad = int(body.get('cantidad', 0))
        except (TypeError, ValueError):
            return create_response(400, "Cantidad inválida.")
        if cantidad == 0:
            return create_response(400, "La cantidad de ajuste no puede ser 0.")

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

        # Si es decremento, validar que el stock resultante no quede negativo (atómicamente)
        if cantidad < 0:
            stock_minimo_requerido = -cantidad
            update_query = {**query, "stock": {"$gte": stock_minimo_requerido}}
        else:
            update_query = query

        result = db["items"].find_one_and_update(
            update_query,
            {"$inc": {"stock": cantidad}, "$set": {"updatedAt": datetime.utcnow().isoformat() + "Z"}},
            return_document=True
        )

        if not result:
            return create_response(400, f"Stock insuficiente. Disponible: {item.get('stock', 0)}, Solicitado: {-cantidad}")

        # Bitácora de movimientos de inventario (auditoría)
        try:
            db["inventario_movimientos"].insert_one({
                "tenant_id": tenant_id,
                "item_id": item_id,
                "sucursal_id": sucursal_id,
                "cantidad": cantidad,
                "stock_resultante": result.get('stock', 0),
                "concepto": body.get('concepto') or ("AJUSTE_MANUAL" if cantidad > 0 else "MERMA"),
                "usuario_id": claims.get('sub'),
                "usuario_nombre": claims.get('name') or claims.get('email'),
                "createdAt": datetime.utcnow()
            })
        except Exception as bit_err:
            logger.warning(f"No se pudo registrar bitácora de inventario: {bit_err}")

        result['id'] = str(result.pop('_id'))

        return create_response(200, "Stock actualizado", {
            "nuevo_stock": result['stock'],
            "ajuste": cantidad
        })
    except Exception as e:
        return handle_exception(e)
