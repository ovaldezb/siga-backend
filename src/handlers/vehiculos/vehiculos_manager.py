import json
from bson import ObjectId
from datetime import datetime
from aws_lambda_powertools import Logger
from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.infrastructure.database import get_tenant_db
from src.shared.utils.auth_utils import try_parse_id, parse_object_id, get_claims
from src.shared.utils.indexes import ensure_indexes
from src.shared.utils.date_utils import iso_utc

logger = Logger()

@logger.inject_lambda_context
def list_vehiculos_handler(event, context):
    """GET /vehiculos?cliente_id=xxx&page=1&limit=25 — Lista vehículos con paginación.

    Plan de mantenimiento (item #9): cuando se pasa `mantenimiento=pronto|vencido`,
    se calculan en pipeline `mantenimiento_status`, `dias_desde_ultima_visita` y
    `km_para_aceite` para cada vehículo y se filtra por el estado pedido. Los
    umbrales son configurables vía query params (`km_umbral`, `dias_pronto`,
    `dias_vencido`). El estado siempre se devuelve si existe, aunque no se filtre,
    para que el front pueda pintar badges en la lista normal.
    """
    try:
        claims =get_claims(event)
        tenant_id = claims.get('custom:tenant_id')

        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        query_params = event.get('queryStringParameters') or {}
        cliente_id = query_params.get('cliente_id', '').strip()
        search = query_params.get('search', '').strip()
        sucursal_id = query_params.get('sucursalId')

        # Plan de mantenimiento (item #9)
        mantenimiento_filter = (query_params.get('mantenimiento') or '').strip().lower()
        if mantenimiento_filter not in ('', 'pronto', 'vencido'):
            mantenimiento_filter = ''
        try:
            km_umbral = int(query_params.get('km_umbral', 500))
        except (TypeError, ValueError):
            km_umbral = 500
        try:
            dias_pronto = int(query_params.get('dias_pronto', 180))
        except (TypeError, ValueError):
            dias_pronto = 180
        try:
            dias_vencido = int(query_params.get('dias_vencido', 365))
        except (TypeError, ValueError):
            dias_vencido = 365

        # Paginación
        page = int(query_params.get('page', 1))
        limit = int(query_params.get('limit', 25))
        skip = (page - 1) * limit

        db = get_tenant_db(tenant_id)
        ensure_indexes(db, tenant_id)

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

        # Stages comunes (lookup cliente + cálculo de mantenimiento). Usados tanto
        # para count como para la página: así el total cuadra con el filtro post-pipeline.
        mantenimiento_stages = [
            {
                "$lookup": {
                    "from": "clientes",
                    "let": {"cid": "$cliente_id"},
                    "pipeline": [
                        {"$match": {"$expr": {"$or": [
                            {"$eq": ["$_id", "$$cid"]},
                            {"$eq": ["$_id", {"$toObjectId": "$$cid"}]}
                        ]}}},
                        {"$project": {"nombre": 1, "apellido_paterno": 1, "apellido_materno": 1, "telefono": 1}}
                    ],
                    "as": "cliente_info"
                }
            },
            {
                "$lookup": {
                    "from": "ordenes_servicio",
                    "let": {"vid": {"$toString": "$_id"}},
                    "pipeline": [
                        {"$match": {"$expr": {"$eq": ["$vehiculo_id", "$$vid"]}}},
                        {"$sort": {"createdAt": -1}},
                        {"$limit": 1},
                        {"$project": {
                            "_id": 0,
                            "createdAt": 1,
                            "proximo_cambio_aceite": 1,
                            "proximo_cambio_bujias": 1,
                            "proximo_cambio_aceite_anterior": 1,
                            "proximo_cambio_bujias_anterior": 1,
                        }}
                    ],
                    "as": "ultima_os"
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
                    },
                    "cliente_telefono": {"$arrayElemAt": ["$cliente_info.telefono", 0]},
                    "ultima_visita_at": {"$arrayElemAt": ["$ultima_os.createdAt", 0]},
                    # Tomar de la última OS los valores de mantenimiento si no están en el vehículo
                    "proximo_cambio_aceite": {"$cond": {
                        "if": {"$gt": [{"$ifNull": ["$proximo_cambio_aceite", 0]}, 0]},
                        "then": "$proximo_cambio_aceite",
                        "else": {"$arrayElemAt": ["$ultima_os.proximo_cambio_aceite", 0]}
                    }},
                    "proximo_cambio_bujias": {"$cond": {
                        "if": {"$gt": [{"$ifNull": ["$proximo_cambio_bujias", 0]}, 0]},
                        "then": "$proximo_cambio_bujias",
                        "else": {"$arrayElemAt": ["$ultima_os.proximo_cambio_bujias", 0]}
                    }}
                }
            },
            {
                "$addFields": {
                    "km_para_aceite": {
                        "$cond": {
                            "if": {"$and": [
                                {"$gt": [{"$ifNull": ["$proximo_cambio_aceite", 0]}, 0]},
                                {"$gt": [{"$ifNull": ["$kilometraje", 0]}, 0]}
                            ]},
                            "then": {"$subtract": ["$proximo_cambio_aceite", "$kilometraje"]},
                            "else": None
                        }
                    },
                    "dias_desde_ultima_visita": {
                        "$cond": {
                            "if": {"$ifNull": ["$ultima_visita_at", False]},
                            "then": {"$dateDiff": {
                                "startDate": "$ultima_visita_at",
                                "endDate": "$$NOW",
                                "unit": "day"
                            }},
                            "else": None
                        }
                    }
                }
            },
            {
                "$addFields": {
                    "mantenimiento_status": {
                        "$switch": {
                            "branches": [
                                {"case": {"$or": [
                                    {"$and": [
                                        {"$ne": ["$km_para_aceite", None]},
                                        {"$lte": ["$km_para_aceite", 0]}
                                    ]},
                                    {"$and": [
                                        {"$ne": ["$dias_desde_ultima_visita", None]},
                                        {"$gte": ["$dias_desde_ultima_visita", dias_vencido]}
                                    ]}
                                ]}, "then": "vencido"},
                                {"case": {"$or": [
                                    {"$and": [
                                        {"$ne": ["$km_para_aceite", None]},
                                        {"$lte": ["$km_para_aceite", km_umbral]}
                                    ]},
                                    {"$and": [
                                        {"$ne": ["$dias_desde_ultima_visita", None]},
                                        {"$gte": ["$dias_desde_ultima_visita", dias_pronto]}
                                    ]}
                                ]}, "then": "pronto"}
                            ],
                            "default": None
                        }
                    }
                }
            },
            {"$project": {"cliente_info": 0, "ultima_os": 0, "ultima_visita_at": 0}}
        ]

        # Filtro post-cálculo (solo si se pidió mantenimiento). Único path que
        # cambia el total: si lo aplicamos, contar requiere también recorrer pipeline.
        match_mantenimiento = []
        if mantenimiento_filter:
            match_mantenimiento = [{"$match": {"mantenimiento_status": mantenimiento_filter}}]

        # Total de registros para el filtro dado
        if mantenimiento_filter:
            count_pipeline = [{"$match": filtro}] + mantenimiento_stages + match_mantenimiento + [{"$count": "total"}]
            count_res = list(db["vehiculos"].aggregate(count_pipeline))
            total = count_res[0]['total'] if count_res else 0
        else:
            total = db["vehiculos"].count_documents(filtro)

        # Cuando no hay filtro de mantenimiento, recortamos la página ANTES del
        # lookup para no resolver cliente_info + última OS sobre la flota completa.
        # Con filtro, los stages tienen que correr primero porque el match depende
        # del campo calculado.
        if mantenimiento_filter:
            pipeline = (
                [{"$match": filtro}]
                + mantenimiento_stages
                + match_mantenimiento
                + [{"$sort": {"createdAt": -1}}, {"$skip": skip}, {"$limit": limit}]
            )
        else:
            pipeline = (
                [{"$match": filtro}, {"$sort": {"createdAt": -1}}, {"$skip": skip}, {"$limit": limit}]
                + mantenimiento_stages
            )

        cursor = db["vehiculos"].aggregate(pipeline)

        vehiculos = []
        for v in cursor:
            v['id'] = str(v.pop('_id'))
            if 'sucursal_id' in v:
                v['sucursalId'] = v.pop('sucursal_id')
            # Compat datos legacy: documentos viejos guardados con 'año'
            if 'año' in v and 'anio' not in v:
                v['anio'] = v.pop('año')
            elif 'año' in v:
                v.pop('año')
            if 'createdAt' in v and isinstance(v['createdAt'], datetime):
                v['createdAt'] = iso_utc(v['createdAt'])
            if 'updatedAt' in v and isinstance(v['updatedAt'], datetime):
                v['updatedAt'] = iso_utc(v['updatedAt'])
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
        claims =get_claims(event)
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
        # Compat datos legacy: documentos viejos guardados con 'año'
        if 'año' in vehiculo and 'anio' not in vehiculo:
            vehiculo['anio'] = vehiculo.pop('año')
        elif 'año' in vehiculo:
            vehiculo.pop('año')
        if 'createdAt' in vehiculo and isinstance(vehiculo['createdAt'], datetime):
            vehiculo['createdAt'] = iso_utc(vehiculo['createdAt'])
        if 'updatedAt' in vehiculo and isinstance(vehiculo['updatedAt'], datetime):
            vehiculo['updatedAt'] = iso_utc(vehiculo['updatedAt'])

        return create_response(200, "Vehículo obtenido", vehiculo)

    except Exception as e:
        return handle_exception(e)

@logger.inject_lambda_context
def create_vehiculo_handler(event, context):
    """POST /vehiculos — Crea un nuevo vehículo."""
    try:
        claims =get_claims(event)
        tenant_id = claims.get('custom:tenant_id')

        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        body = json.loads(event.get('body', '{}'))
        db = get_tenant_db(tenant_id)

        # VALIDACIÓN ESTRICTA — placas y VIN son opcionales (se pueden capturar después).
        required = ["marca", "modelo", "cliente_id", "sucursalId"]
        for field in required:
            if not body.get(field):
                return create_response(400, f"El campo '{field}' es obligatorio para registrar un vehículo.")

        db = get_tenant_db(tenant_id)

        # Crear objeto base con campos obligatorios
        nuevo_vehiculo = {
            "marca": body['marca'],
            "modelo": body['modelo'],
            "placas": body.get('placas', ''),
            "cliente_id": body['cliente_id'],
            "sucursal_id": body['sucursalId'],
            "tenant_id": tenant_id,
            "createdAt": datetime.utcnow()
        }

        # Normalizar 'año' → 'anio' antes de mezclar (canónico = anio sin tilde)
        if 'año' in body and 'anio' not in body:
            body['anio'] = body.pop('año')
        elif 'año' in body:
            body.pop('año')  # ya tenemos anio, descartar duplicado

        # Mezclar con el resto de los campos del body (color, vin, anio, kilometraje, etc.)
        # EXCLUIMOS campos de UI como 'cliente_nombre' para no ensuciar la DB
        for k, v in body.items():
            if k not in ['id', '_id', 'tenant_id', 'createdAt', 'marca', 'modelo', 'placas', 'cliente_id', 'cliente_nombre', 'año']:
                nuevo_vehiculo[k] = v

        result = db["vehiculos"].insert_one(nuevo_vehiculo)
        nuevo_vehiculo['id'] = str(result.inserted_id)
        del nuevo_vehiculo['_id']
        if 'sucursal_id' in nuevo_vehiculo:
            nuevo_vehiculo['sucursalId'] = nuevo_vehiculo.pop('sucursal_id')
        nuevo_vehiculo['createdAt'] = iso_utc(nuevo_vehiculo['createdAt'])

        return create_response(201, "Vehículo creado exitosamente", nuevo_vehiculo)

    except Exception as e:
        return handle_exception(e)

@logger.inject_lambda_context
def delete_vehiculo_handler(event, context):
    """DELETE /vehiculos/{id} — Elimina un vehículo (bloqueado si tiene OS)."""
    try:
        claims =get_claims(event)
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

        # Snapshot del cliente para sincronizar vehiculos_resumen tras el delete.
        vehiculo_doc = db["vehiculos"].find_one({"_id": ObjectId(vehiculo_id)}, {"cliente_id": 1})

        result = db["vehiculos"].delete_one({"_id": ObjectId(vehiculo_id)})
        if result.deleted_count == 0:
            return create_response(404, "Vehículo no encontrado.")

        # Mantener consistente el array vehiculos_resumen del cliente
        if vehiculo_doc and vehiculo_doc.get("cliente_id"):
            try:
                _sync_vehiculos_resumen_pull(db, vehiculo_doc["cliente_id"], vehiculo_id)
            except Exception as sync_err:
                logger.warning(f"No se pudo sincronizar vehiculos_resumen al borrar {vehiculo_id}: {sync_err}")

        return create_response(200, "Vehículo eliminado")

    except Exception as e:
        return handle_exception(e)


def _sync_vehiculos_resumen_pull(db, cliente_id, vehiculo_id):
    """Quita la entry de vehiculos_resumen que coincida con vehiculo_id."""
    cliente_oid, err = parse_object_id(cliente_id)
    if err:
        return
    db["clientes"].update_one(
        {"_id": cliente_oid},
        {"$pull": {"vehiculos_resumen": {"id": vehiculo_id}}}
    )


def _sync_vehiculos_resumen_update(db, cliente_id, vehiculo_id, vehiculo_doc):
    """Refresca la entry de vehiculos_resumen del cliente con los campos visibles del vehículo."""
    cliente_oid, err = parse_object_id(cliente_id)
    if err:
        return

    nueva_entry = {
        "id": vehiculo_id,
        "placas": vehiculo_doc.get("placas"),
        "marca": vehiculo_doc.get("marca"),
        "modelo": vehiculo_doc.get("modelo"),
        "anio": vehiculo_doc.get("anio"),
    }

    # Si ya existe la entry, refrescarla; si no, hacer push (legacy con resumen vacío)
    res = db["clientes"].update_one(
        {"_id": cliente_oid, "vehiculos_resumen.id": vehiculo_id},
        {"$set": {
            "vehiculos_resumen.$.placas": nueva_entry["placas"],
            "vehiculos_resumen.$.marca": nueva_entry["marca"],
            "vehiculos_resumen.$.modelo": nueva_entry["modelo"],
            "vehiculos_resumen.$.anio": nueva_entry["anio"],
        }}
    )
    if res.matched_count == 0:
        db["clientes"].update_one(
            {"_id": cliente_oid},
            {"$push": {"vehiculos_resumen": nueva_entry}}
        )


@logger.inject_lambda_context
def update_vehiculo_handler(event, context):
    """PUT /vehiculos/{id} — Actualiza un vehículo existente."""
    try:
        claims =get_claims(event)
        tenant_id = claims.get('custom:tenant_id')

        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        vehiculo_id = event['pathParameters']['id']
        body = json.loads(event.get('body', '{}'))
        db = get_tenant_db(tenant_id)

        # Normalizar 'año' → 'anio'
        if 'año' in body:
            body['anio'] = body.pop('año')

        # Limpiar datos para el update (evitar cambiar IDs, tenant o reasignar dueño).
        # cliente_id se bloquea: cambiar el dueño de un vehículo requiere un endpoint
        # dedicado con auditoría (no se puede colar por el PUT genérico).
        update_data = {k: v for k, v in body.items() if k not in ['id', '_id', 'tenant_id', 'cliente_id', 'createdAt', 'cliente_nombre']}

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

        # Sincronizar vehiculos_resumen del cliente si los campos visibles cambiaron
        if updated_vehiculo and updated_vehiculo.get("cliente_id"):
            campos_visibles = {"placas", "marca", "modelo", "anio"}
            if any(c in update_data for c in campos_visibles):
                try:
                    _sync_vehiculos_resumen_update(
                        db, updated_vehiculo["cliente_id"], vehiculo_id, updated_vehiculo
                    )
                except Exception as sync_err:
                    logger.warning(f"No se pudo sincronizar vehiculos_resumen al actualizar {vehiculo_id}: {sync_err}")

        updated_vehiculo['id'] = str(updated_vehiculo.pop('_id'))
        if 'sucursal_id' in updated_vehiculo:
            updated_vehiculo['sucursalId'] = updated_vehiculo.pop('sucursal_id')
        if 'createdAt' in updated_vehiculo and isinstance(updated_vehiculo['createdAt'], datetime):
            updated_vehiculo['createdAt'] = iso_utc(updated_vehiculo['createdAt'])
        if 'updatedAt' in updated_vehiculo and isinstance(updated_vehiculo['updatedAt'], datetime):
            updated_vehiculo['updatedAt'] = iso_utc(updated_vehiculo['updatedAt'])

        return create_response(200, "Vehículo actualizado exitosamente", updated_vehiculo)

    except Exception as e:
        return handle_exception(e)
