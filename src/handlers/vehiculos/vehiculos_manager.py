import json
from bson import ObjectId
from datetime import datetime
from aws_lambda_powertools import Logger
from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.infrastructure.database import get_tenant_db

logger = Logger()

@logger.inject_lambda_context
def list_vehiculos_handler(event, context):
    """GET /vehiculos?cliente_id=xxx&page=1&limit=25 — Lista vehículos con paginación."""
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')

        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        query_params = event.get('queryStringParameters') or {}
        cliente_id = query_params.get('cliente_id', '').strip()
        search = query_params.get('search', '').strip()
        sucursal_id = query_params.get('sucursalId')
        
        # Paginación
        page = int(query_params.get('page', 1))
        limit = int(query_params.get('limit', 25))
        skip = (page - 1) * limit

        db = get_tenant_db(tenant_id)

        # Filtro base
        filtro = {}
        if sucursal_id:
            filtro["sucursal_id"] = sucursal_id

        if cliente_id:
            filtro["cliente_id"] = cliente_id
            
        if search:
            regex = {"$regex": search, "$options": "i"}
            search_filters = [
                {"marca": regex},
                {"modelo": regex},
                {"placas": regex}
            ]
            if filtro:
                if "$or" in filtro: # Por si acaso
                    filtro = {"$and": [filtro, {"$or": search_filters}]}
                else:
                    filtro["$or"] = search_filters
            else:
                filtro["$or"] = search_filters
        
        # Total de registros para el filtro dado
        total = db["vehiculos"].count_documents(filtro)
        
        # Pipeline de agregación para incluir info del cliente
        pipeline = [
            {"$match": filtro},
            {"$sort": {"createdAt": -1}},
            {"$skip": skip},
            {"$limit": limit},
            {
                "$addFields": {
                    "cliente_oid": {
                        "$convert": {
                            "input": "$cliente_id",
                            "to": "objectId",
                            "onError": "$cliente_id",
                            "onNull": None
                        }
                    }
                }
            },
            {
                "$lookup": {
                    "from": "clientes",
                    "localField": "cliente_oid",
                    "foreignField": "_id",
                    "as": "cliente_info"
                }
            },
            {
                "$addFields": {
                    "cliente_nombre": {
                        "$cond": {
                            "if": {"$gt": [{"$size": "$cliente_info"}, 0]},
                            "then": {
                                "$concat": [
                                    {"$ifNull": [{"$arrayElemAt": ["$cliente_info.nombre", 0]}, ""]},
                                    " ",
                                    {"$ifNull": [{"$arrayElemAt": ["$cliente_info.apellido_paterno", 0]}, ""]},
                                    " ",
                                    {"$ifNull": [{"$arrayElemAt": ["$cliente_info.apellido_materno", 0]}, ""]}
                                ]
                            },
                            "else": "Cliente Desconocido"
                        }
                    }
                }
            },
            {"$project": {"cliente_info": 0, "cliente_oid": 0}}
        ]
        
        cursor = db["vehiculos"].aggregate(pipeline)

        vehiculos = []
        for v in cursor:
            v['id'] = str(v.pop('_id'))
            if 'sucursal_id' in v:
                v['sucursalId'] = v.pop('sucursal_id')
            if 'createdAt' in v and isinstance(v['createdAt'], datetime):
                v['createdAt'] = v['createdAt'].isoformat()
            if 'updatedAt' in v and isinstance(v['updatedAt'], datetime):
                v['updatedAt'] = v['updatedAt'].isoformat()
            vehiculos.append(v)

        # Respuesta estructurada para paginación
        resultado = {
            "items": vehiculos,
            "total": total,
            "page": page,
            "limit": limit,
            "totalPages": (total + limit - 1) // limit
        }

        return create_response(200, "Vehículos obtenidos", resultado)

    except Exception as e:
        return handle_exception(e)

@logger.inject_lambda_context
def get_vehiculo_handler(event, context):
    """GET /vehiculos/{id}  — Obtiene un vehículo por su _id de MongoDB."""
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')

        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        vehiculo_id = event['pathParameters']['id']
        db = get_tenant_db(tenant_id)

        # Pipeline de agregación para incluir info del cliente
        pipeline = [
            {"$match": {"_id": ObjectId(vehiculo_id)}},
            {
                "$addFields": {
                    "cliente_oid": {
                        "$convert": {
                            "input": "$cliente_id",
                            "to": "objectId",
                            "onError": "$cliente_id",
                            "onNull": None
                        }
                    }
                }
            },
            {
                "$lookup": {
                    "from": "clientes",
                    "localField": "cliente_oid",
                    "foreignField": "_id",
                    "as": "cliente_info"
                }
            },
            {
                "$addFields": {
                    "cliente_nombre": {
                        "$cond": {
                            "if": {"$gt": [{"$size": "$cliente_info"}, 0]},
                            "then": {
                                "$concat": [
                                    {"$ifNull": [{"$arrayElemAt": ["$cliente_info.nombre", 0]}, ""]},
                                    " ",
                                    {"$ifNull": [{"$arrayElemAt": ["$cliente_info.apellido_paterno", 0]}, ""]},
                                    " ",
                                    {"$ifNull": [{"$arrayElemAt": ["$cliente_info.apellido_materno", 0]}, ""]}
                                ]
                            },
                            "else": "Cliente Desconocido"
                        }
                    }
                }
            },
            {"$project": {"cliente_info": 0, "cliente_oid": 0}}
        ]
        
        resultado = list(db["vehiculos"].aggregate(pipeline))

        if not resultado:
            return create_response(404, "Vehículo no encontrado.")

        vehiculo = resultado[0]
        vehiculo['id'] = str(vehiculo.pop('_id'))
        if 'sucursal_id' in vehiculo:
            vehiculo['sucursalId'] = vehiculo.pop('sucursal_id')
        if 'createdAt' in vehiculo and isinstance(vehiculo['createdAt'], datetime):
            vehiculo['createdAt'] = vehiculo['createdAt'].isoformat()
        if 'updatedAt' in vehiculo and isinstance(vehiculo['updatedAt'], datetime):
            vehiculo['updatedAt'] = vehiculo['updatedAt'].isoformat()

        return create_response(200, "Vehículo obtenido", vehiculo)

    except Exception as e:
        return handle_exception(e)

@logger.inject_lambda_context
def create_vehiculo_handler(event, context):
    """POST /vehiculos — Crea un nuevo vehículo."""
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')

        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        body = json.loads(event.get('body', '{}'))
        db = get_tenant_db(tenant_id)

        # VALIDACIÓN ESTRICTA
        required = ["marca", "modelo", "placas", "cliente_id", "sucursalId"]
        for field in required:
            if not body.get(field):
                return create_response(400, f"El campo '{field}' es obligatorio para registrar un vehículo.")

        db = get_tenant_db(tenant_id)

        # Crear objeto base con campos obligatorios
        nuevo_vehiculo = {
            "marca": body['marca'],
            "modelo": body['modelo'],
            "placas": body['placas'],
            "cliente_id": body['cliente_id'],
            "sucursal_id": body['sucursalId'],
            "tenant_id": tenant_id,
            "createdAt": datetime.utcnow()
        }

        # Mezclar con el resto de los campos del body (color, vin, anio, kilometraje, etc.)
        # EXCLUIMOS campos de UI como 'cliente_nombre' para no ensuciar la DB
        for k, v in body.items():
            if k not in ['id', '_id', 'tenant_id', 'createdAt', 'marca', 'modelo', 'placas', 'cliente_id', 'cliente_nombre']:
                nuevo_vehiculo[k] = v

        result = db["vehiculos"].insert_one(nuevo_vehiculo)
        nuevo_vehiculo['id'] = str(result.inserted_id)
        del nuevo_vehiculo['_id']
        if 'sucursal_id' in nuevo_vehiculo:
            nuevo_vehiculo['sucursalId'] = nuevo_vehiculo.pop('sucursal_id')
        nuevo_vehiculo['createdAt'] = nuevo_vehiculo['createdAt'].isoformat()

        return create_response(201, "Vehículo creado exitosamente", nuevo_vehiculo)

    except Exception as e:
        return handle_exception(e)

@logger.inject_lambda_context
def delete_vehiculo_handler(event, context):
    """DELETE /vehiculos/{id} — Elimina un vehículo (bloqueado si tiene OS)."""
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')

        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        vehiculo_id = event['pathParameters']['id']
        db = get_tenant_db(tenant_id)

        ordenes_count = db["ordenes_servicio"].count_documents({"vehiculo_id": vehiculo_id})
        if ordenes_count > 0:
            return create_response(
                409,
                f"No se puede eliminar: el vehículo tiene {ordenes_count} orden(es) de servicio asociadas."
            )

        result = db["vehiculos"].delete_one({"_id": ObjectId(vehiculo_id)})
        if result.deleted_count == 0:
            return create_response(404, "Vehículo no encontrado.")

        return create_response(200, "Vehículo eliminado")

    except Exception as e:
        return handle_exception(e)


@logger.inject_lambda_context
def update_vehiculo_handler(event, context):
    """PUT /vehiculos/{id} — Actualiza un vehículo existente."""
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')

        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        vehiculo_id = event['pathParameters']['id']
        body = json.loads(event.get('body', '{}'))
        db = get_tenant_db(tenant_id)

        # Limpiar datos para el update (evitar cambiar IDs o tenant)
        update_data = {k: v for k, v in body.items() if k not in ['id', '_id', 'tenant_id', 'createdAt', 'cliente_nombre']}
        
        # Mapear sucursalId (camelCase FE) a sucursal_id (snake_case DB)
        if 'sucursalId' in update_data:
            update_data['sucursal_id'] = update_data.pop('sucursalId')
            
        update_data['updatedAt'] = datetime.utcnow()

        result = db["vehiculos"].update_one(
            {"_id": ObjectId(vehiculo_id)},
            {"$set": update_data}
        )

        if result.matched_count == 0:
            return create_response(404, "Vehículo no encontrado.")

        # Obtener el objeto actualizado para devolverlo completo
        updated_vehiculo = db["vehiculos"].find_one({"_id": ObjectId(vehiculo_id)})
        updated_vehiculo['id'] = str(updated_vehiculo.pop('_id'))
        if 'sucursal_id' in updated_vehiculo:
            updated_vehiculo['sucursalId'] = updated_vehiculo.pop('sucursal_id')
        if 'createdAt' in updated_vehiculo and isinstance(updated_vehiculo['createdAt'], datetime):
            updated_vehiculo['createdAt'] = updated_vehiculo['createdAt'].isoformat()
        if 'updatedAt' in updated_vehiculo and isinstance(updated_vehiculo['updatedAt'], datetime):
            updated_vehiculo['updatedAt'] = updated_vehiculo['updatedAt'].isoformat()

        return create_response(200, "Vehículo actualizado exitosamente", updated_vehiculo)

    except Exception as e:
        return handle_exception(e)
