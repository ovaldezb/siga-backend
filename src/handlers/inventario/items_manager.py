import json
from datetime import datetime
from aws_lambda_powertools import Logger
from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.infrastructure.database import get_tenant_db
from src.shared.utils.auth_utils import try_parse_id, resolve_sucursal_scope, is_admin, get_claims
from bson import ObjectId
from bson.errors import InvalidId
from src.shared.utils.date_utils import iso_utc

logger = Logger()

def list_items_handler(event, context):
    try:
        claims =get_claims(event)
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
        claims =get_claims(event)
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
        costo_compra_inicial = to_float(body.get('precio_compra', 0))
        nuevo_item = {
            "tipo": tipo,
            "nombre": body['nombre'],
            "no_parte": body['no_parte'] or body.get('noParte'),
            "precio_venta": to_float(body['precio_venta']),
            "precio_taller": to_float(body.get('precio_taller', 0)),
            "precio_cliente": to_float(body.get('precio_cliente', 0)),
            "precio_distribuidor": to_float(body.get('precio_distribuidor', 0)),
            "precio_compra": costo_compra_inicial,
            # costo_promedio: arranca = precio_compra inicial; lo recalcula compras_manager
            "costo_promedio": to_float(body.get('costo_promedio', costo_compra_inicial)),
            # Flags fiscales: por defecto los precios capturados YA incluyen IVA (convención SIGA)
            "precio_incluye_iva": bool(body.get('precio_incluye_iva', True)),
            "iva_exento": bool(body.get('iva_exento', False)),
            "categoria": body.get('categoria', 'GENERAL'),
            "marca": body.get('marca', ''),
            "proveedor": body.get('proveedor', ''),
            "proveedor_id": body.get('proveedor_id') or body.get('proveedorId'),
            "sucursal_id": body.get('sucursalId') or body.get('sucursal_id'),
            "tenant_id": tenant_id,
            "createdAt": iso_utc(),
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

def inyectar_catalogo_handler(event, context):
    """POST /items/inyectar — Replica el catálogo de artículos de una sucursal origen
    hacia una o varias sucursales destino, SIN inventario (stock 0).

    Solo ADMIN. Body: { origen_id, destinos: [sucursal_id, ...] }.
    Copia únicamente artículos con número de parte. Por cada destino omite los
    no_parte que ya existan ahí (no toca el artículo existente: conserva precios y
    stock locales). Mismo criterio que el clon de items en la recepción de traspasos.
    """
    try:
        claims = get_claims(event)
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")
        if not is_admin(claims):
            return create_response(403, "Solo un administrador puede inyectar el catálogo entre sucursales.")

        body = json.loads(event.get('body', '{}'))
        origen_id = body.get('origen_id') or body.get('origenId')
        destinos = body.get('destinos') or body.get('destinosIds') or []
        if isinstance(destinos, str):
            destinos = [destinos]
        # Normalizar: strings no vacíos, sin el propio origen, sin repetidos
        destinos = [d for d in {str(d) for d in destinos if d} if d != str(origen_id)]

        if not origen_id:
            return create_response(400, "Debe indicar la sucursal origen.")
        if not destinos:
            return create_response(400, "Debe indicar al menos una sucursal destino distinta del origen.")

        db = get_tenant_db(tenant_id)

        # Solo artículos con número de parte (catálogo de productos identificables)
        origen_items = list(db["items"].find({
            "sucursal_id": origen_id,
            "no_parte": {"$nin": [None, ""]}
        }))
        if not origen_items:
            return create_response(404, "La sucursal origen no tiene artículos con número de parte para inyectar.")

        resumen = []
        for destino_id in destinos:
            # no_parte ya presentes en el destino (en minúsculas para comparar sin importar mayúsculas)
            existentes = set()
            for d in db["items"].find({"sucursal_id": destino_id}, {"no_parte": 1}):
                np = (d.get('no_parte') or '').strip().lower()
                if np:
                    existentes.add(np)

            nuevos = []
            for it in origen_items:
                np = (it.get('no_parte') or '').strip().lower()
                if not np or np in existentes:
                    continue
                existentes.add(np)  # evita duplicar si el origen trae el mismo no_parte repetido
                clone = {k: v for k, v in it.items() if k != '_id'}
                clone['sucursal_id'] = destino_id
                clone['tenant_id'] = tenant_id
                # Catálogo sin inventario: stock 0 para productos, None para servicios
                if clone.get('tipo') == 'SERVICIO' or not clone.get('maneja_inventario'):
                    clone['stock'] = None
                else:
                    clone['stock'] = 0
                clone['createdAt'] = iso_utc()
                clone['clonado_de'] = str(it['_id'])
                nuevos.append(clone)

            creados = 0
            if nuevos:
                try:
                    res = db["items"].insert_many(nuevos)
                    creados = len(res.inserted_ids)
                except Exception as ins_err:
                    logger.error(f"Error inyectando catálogo a sucursal {destino_id}: {ins_err}")
                    return create_response(500, f"No se pudo inyectar el catálogo a la sucursal destino: {ins_err}")

            resumen.append({
                "sucursal_id": destino_id,
                "creados": creados,
                "omitidos": len(origen_items) - creados
            })

        total_creados = sum(r["creados"] for r in resumen)
        return create_response(200, f"Catálogo inyectado: {total_creados} artículo(s) creado(s).", {
            "total_creados": total_creados,
            "resumen": resumen
        })
    except Exception as e:
        return handle_exception(e)


def get_item_handler(event, context):
    try:
        claims =get_claims(event)
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
        claims =get_claims(event)
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
            "precio_compra", "costo_promedio", "precio_incluye_iva", "iva_exento",
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

        update_data['updatedAt'] = iso_utc()

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
        claims =get_claims(event)
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
        claims =get_claims(event)
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
        
        logger.info(f"Actualizando stock de item {item_id}. Sucursal enviada: {sucursal_id}")

        db = get_tenant_db(tenant_id)

        # Buscar item y validar que maneja inventario y sucursal
        query = {"_id": ObjectId(item_id)}
        if sucursal_id:
            query["sucursal_id"] = sucursal_id
        
        logger.info(f"Query para buscar item: {query}")
            
        item = db["items"].find_one(query)
        if not item:
            # Buscar sin sucursal para diagnosticar
            item_diagnostico = db["items"].find_one({"_id": ObjectId(item_id)})
            if item_diagnostico:
                logger.warning(f"Item encontrado pero sucursal no coincide. DB sucursal: {item_diagnostico.get('sucursal_id')}")
            else:
                logger.warning(f"Item {item_id} ni siquiera existe con ese ID.")
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
            {"$inc": {"stock": cantidad}, "$set": {"updatedAt": iso_utc()}},
            return_document=True
        )

        if not result:
            return create_response(400, f"Stock insuficiente. Disponible: {item.get('stock', 0)}, Solicitado: {-cantidad}")

        # Bitácora de movimientos de inventario (auditoría)
        try:
            db["inventario_movimientos"].insert_one({
                "tenant_id": tenant_id,
                "item_id": item_id,
                "item_nombre": item.get('nombre'),
                "sucursal_id": sucursal_id,
                "cantidad": cantidad,
                "stock_anterior": item.get('stock', 0),
                "stock_resultante": result.get('stock', 0),
                "concepto": body.get('concepto') or ("AJUSTE_MANUAL" if cantidad > 0 else "MERMA"),
                "motivo": body.get('motivo') or '',
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


def list_inventario_movimientos_handler(event, context):
    """GET /items/movimientos — Historial de movimientos de inventario (kardex).

    Lista la bitácora `inventario_movimientos` (ajustes manuales, mermas, compras, etc.)
    Filtros opcionales por query string: item_id, sucursal_id, concepto, desde, hasta.
    Paginado con page/limit. Enriquece cada fila con el nombre del item cuando falta.
    """
    try:
        claims = get_claims(event)
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        qp = event.get('queryStringParameters') or {}
        page = max(1, int(qp.get('page', 1) or 1))
        limit = min(int(qp.get('limit', 50) or 50), 200)
        skip = (page - 1) * limit

        query = {"tenant_id": tenant_id}
        if qp.get('item_id'):
            query['item_id'] = qp['item_id']
        sucursal_id = qp.get('sucursal_id') or qp.get('sucursalId')
        if sucursal_id:
            query['sucursal_id'] = sucursal_id
        if qp.get('concepto'):
            query['concepto'] = qp['concepto'].upper()

        # Rango de fechas sobre createdAt (datetime). Acepta YYYY-MM-DD.
        rango = {}
        if qp.get('desde'):
            try:
                rango['$gte'] = datetime.strptime(qp['desde'][:10], '%Y-%m-%d')
            except (ValueError, TypeError):
                pass
        if qp.get('hasta'):
            try:
                # hasta inclusivo: sumamos un día
                from datetime import timedelta
                rango['$lt'] = datetime.strptime(qp['hasta'][:10], '%Y-%m-%d') + timedelta(days=1)
            except (ValueError, TypeError):
                pass
        if rango:
            query['createdAt'] = rango

        db = get_tenant_db(tenant_id)
        total = db["inventario_movimientos"].count_documents(query)
        movimientos = list(db["inventario_movimientos"]
                           .find(query)
                           .sort("createdAt", -1)
                           .skip(skip)
                           .limit(limit))

        # Resolver nombres de items que no traen item_nombre (movimientos antiguos).
        faltan_nombre = list({m.get('item_id') for m in movimientos
                              if not m.get('item_nombre') and m.get('item_id')})
        nombres = {}
        if faltan_nombre:
            oids = []
            for iid in faltan_nombre:
                try:
                    oids.append(ObjectId(iid))
                except (InvalidId, TypeError):
                    pass
            if oids:
                for it in db["items"].find({"_id": {"$in": oids}}, {"nombre": 1}):
                    nombres[str(it['_id'])] = it.get('nombre')

        for m in movimientos:
            m['id'] = str(m.pop('_id'))
            if not m.get('item_nombre'):
                m['item_nombre'] = nombres.get(m.get('item_id')) or '(item eliminado)'
            if isinstance(m.get('createdAt'), datetime):
                m['createdAt'] = iso_utc(m['createdAt'])

        return create_response(200, "Movimientos de inventario", {
            "items": movimientos,
            "page": page,
            "limit": limit,
            "total": total,
            "pages": (total + limit - 1) // limit,
        })
    except Exception as e:
        return handle_exception(e)
