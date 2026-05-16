import json
from bson import ObjectId
from datetime import datetime
from aws_lambda_powertools import Logger
from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.infrastructure.database import get_tenant_db
from src.shared.utils.auth_utils import try_parse_id
from src.handlers.admin.folios_manager import _get_next_folio_internal

logger = Logger()


def _calcular_total_orden(puntos_arreglar) -> float:
    """Suma server-side de puntosArreglar.items.(piezas * precioVenta) excluyendo no_cobrar.

    El cliente no puede mandar `total` manipulado: siempre se recalcula aquí para mantener
    consistente lo que se reporta vs la venta POS.
    """
    total = 0.0
    for punto in puntos_arreglar or []:
        for item in (punto.get("items") or []):
            if item.get("no_cobrar") or item.get("rechazado"):
                continue
            try:
                piezas = float(item.get("piezas") or 0)
                precio = float(item.get("precioVenta") or 0)
                total += piezas * precio
            except (TypeError, ValueError):
                continue
    return round(total, 2)


def _ensure_folio_index(db) -> None:
    """Asegura el índice único en (folio) para ordenes_servicio. Idempotente."""
    try:
        db["ordenes_servicio"].create_index(
            [("folio", 1)],
            unique=True,
            partialFilterExpression={"folio": {"$exists": True, "$type": "string"}},
            name="uniq_orden_folio"
        )
    except Exception as idx_err:
        logger.warning(f"No se pudo verificar índice único de folio OS: {idx_err}")

@logger.inject_lambda_context
def create_orden_handler(event, context):
    vehiculo_id = None
    vehiculo_es_nuevo = False
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})

        tenant_id = claims.get('custom:tenant_id')

        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        body = json.loads(event.get('body', '{}'))
        db = get_tenant_db(tenant_id)

        # 0. Validar sucursalId
        sucursal_id_os = body.get("sucursalId")
        if not sucursal_id_os:
            return create_response(400, "El campo 'sucursalId' es obligatorio para crear una orden de servicio.")

        # 1. Generar folio atómico server-side (ignora cualquier folio del body para evitar
        #    spoofing y race conditions). Garantizado único por (tipo, sucursal_id).
        _ensure_folio_index(db)
        folio = _get_next_folio_internal(tenant_id, "os", sucursal_id_os)

        # 2. VEHÍCULO: Existente o Nuevo
        vehiculo_id_recibido = body.get("vehiculo_id", "").strip() if body.get("vehiculo_id") else ""
        vehiculo_data = body.get("vehiculo_snapshot", {})

        if vehiculo_id_recibido:
            # Vehículo ya existe en BD: solo usar el ID
            vehiculo_id = vehiculo_id_recibido
            logger.info(f"Vehículo existente reutilizado: {vehiculo_id}")
        else:
            # Vehículo nuevo: validar datos y crear registro
            if not vehiculo_data:
                return create_response(400, "Los datos del vehículo son requeridos")

            vehiculo_doc = {
                "cliente_id": body.get("cliente_snapshot", {}).get("id"),
                "tenant_id": tenant_id,
                "sucursal_id": sucursal_id_os, # Vinculado a la sucursal de la OS
                "placas": vehiculo_data.get("placas", ""),
                "marca": vehiculo_data.get("marca", ""),
                "modelo": vehiculo_data.get("modelo", ""),
                "anio": vehiculo_data.get("anio"),
                "vin": vehiculo_data.get("vin", ""),
                "color": vehiculo_data.get("color", ""),
                "createdAt": datetime.utcnow(),
            }

            vehiculo_result = db["vehiculos"].insert_one(vehiculo_doc)
            vehiculo_id = vehiculo_result.inserted_id
            vehiculo_es_nuevo = True
            logger.info(f"Vehículo nuevo creado: {vehiculo_id}")

        # 3. Crear la OS con vehiculo_id y bitácora inicial
        estado_inicial = body.get("estado", "RECEPCION")
        responsable = claims.get('email') or claims.get('name') or claims.get('sub') or 'system'
        
        orden_doc = {
            "folio": folio,
            "tenant_id": tenant_id,
            "sucursal_id": sucursal_id_os,
            "estado": estado_inicial,
            "bitacora_estados": [{
                "estado": estado_inicial,
                "fecha": datetime.utcnow().isoformat() + "Z",
                "usuario_id": responsable
            }],
            "cliente_snapshot": body.get("cliente_snapshot"),
            "vehiculo_id": str(vehiculo_id),
            "cita_id": body.get("cita_id"),
            "puntosArreglar": body.get("puntosArreglar", []),
            "falla_reportada": body.get("falla_reportada", ""),
            "diagnostico": body.get("diagnostico", ""),
            "mecanico_id": body.get("mecanico_id"),
            "mecanico_nombre": body.get("mecanico_nombre"),
            "kilometraje": body.get("kilometraje", 0),
            "nivel_tanque": body.get("nivel_tanque", 0),
            "testigos_encendidos": body.get("testigos_encendidos", []),
            "inventario": body.get("inventario", []),
            "proximo_cambio_bujias": body.get("proximo_cambio_bujias", 0),
            "proximo_cambio_aceite": body.get("proximo_cambio_aceite", 0),
            "aplica_costo_revision": body.get("aplica_costo_revision", False),
            "costo_revision": body.get("costo_revision"),
            "anticipo": body.get("anticipo", 0),
            "total": _calcular_total_orden(body.get("puntosArreglar", [])),
            "fechaEstimadaEntrega": body.get("fechaEstimadaEntrega"),
            "createdAt": datetime.utcnow(),
            "updatedAt": datetime.utcnow()
        }

        try:
            orden_result = db["ordenes_servicio"].insert_one(orden_doc)
        except Exception as ins_err:
            # Carrera: el índice único rechazó el folio. Pedimos otro y reintentamos UNA vez.
            if "E11000" in str(ins_err):
                logger.warning(f"Colisión de folio OS {folio}; reintentando con nuevo folio")
                folio = _get_next_folio_internal(tenant_id, "os", sucursal_id_os)
                orden_doc["folio"] = folio
                orden_result = db["ordenes_servicio"].insert_one(orden_doc)
            else:
                raise
        orden_doc["id"] = str(orden_result.inserted_id)
        del orden_doc["_id"]

        # Serializar fechas
        orden_doc["createdAt"] = orden_doc["createdAt"].isoformat()
        orden_doc["updatedAt"] = orden_doc["updatedAt"].isoformat()

        vs = orden_doc.get('vehiculo_snapshot')
        if vs and isinstance(vs, dict):
            if 'sucursal_id' in vs:
                vs['sucursalId'] = vs.pop('sucursal_id')
            if 'createdAt' in vs and isinstance(vs['createdAt'], datetime):
                vs['createdAt'] = vs['createdAt'].isoformat()
            if 'updatedAt' in vs and isinstance(vs['updatedAt'], datetime):
                vs['updatedAt'] = vs['updatedAt'].isoformat()

        # 4. Si viene de una cita, actualizar la cita con la referencia a la OS
        cita_id = body.get("cita_id")
        if cita_id:
            try:
                db["citas"].update_one(
                    {"_id": ObjectId(cita_id)},
                    {"$set": {
                        "orden_id": orden_doc["id"],
                        "estado": "en_proceso",
                        "updatedAt": datetime.utcnow().isoformat()
                    }}
                )
                logger.info(f"Cita {cita_id} vinculada a OS {orden_doc['id']}")
            except Exception as cita_err:
                logger.warning(f"No se pudo actualizar la cita {cita_id}: {cita_err}")

        return create_response(201, "Orden de servicio creada exitosamente", orden_doc)

    except Exception as e:
        # ROLLBACK: Solo si el vehículo fue creado nuevo en esta misma operación
        if vehiculo_es_nuevo and vehiculo_id:
            try:
                db = get_tenant_db(event.get('requestContext', {}).get('authorizer', {}).get('claims', {}).get('custom:tenant_id'))
                db["vehiculos"].delete_one({"_id": vehiculo_id})
                logger.warning(f"ROLLBACK: Vehículo nuevo {vehiculo_id} eliminado por error en creación de OS")
            except Exception as rb_error:
                logger.error(f"Error en rollback del vehículo: {rb_error}")
        return handle_exception(e)

@logger.inject_lambda_context
def list_ordenes_handler(event, context):
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        
        query_params = event.get('queryStringParameters') or {}
        page = int(query_params.get('page', 1))
        limit = int(query_params.get('limit', 20))
        skip = (page - 1) * limit
        
        filter_query = {}
        and_conditions = []
        
        sucursal_id = query_params.get('sucursal_id')
        if sucursal_id:
            and_conditions.append({'sucursal_id': sucursal_id})

        vehiculo_id_filter = query_params.get('vehiculo_id')
        if vehiculo_id_filter:
            filter_query['vehiculo_id'] = vehiculo_id_filter
            
        search_query = query_params.get('q')
        if search_query:
            import re
            regex = re.compile(re.escape(search_query), re.IGNORECASE)
            and_conditions.append({'$or': [
                {"folio": regex},
                {"cliente_snapshot.nombre": regex},
                {"cliente_snapshot.apellido_paterno": regex}
            ]})

        if and_conditions:
            filter_query['$and'] = and_conditions

        db = get_tenant_db(tenant_id)
        
        total = db["ordenes_servicio"].count_documents(filter_query)
        ordenes_cursor = db["ordenes_servicio"].find(filter_query).sort("createdAt", -1).skip(skip).limit(limit)
        
        ordenes_list = list(ordenes_cursor)
        
        # Obtener datos de vehículos por lote para evitar lazy loading en el front
        vehiculo_ids = list(set([o.get('vehiculo_id') for o in ordenes_list if o.get('vehiculo_id')]))
        
        vehiculos_map = {}
        if vehiculo_ids:
            # Buscar por ID
            query_ids = []
            for vid in vehiculo_ids:
                query_ids.append(vid)
                try:
                    query_ids.append(ObjectId(vid))
                except:
                    pass
            vehiculos_data = db["vehiculos"].find({"_id": {"$in": query_ids}})
            for v in vehiculos_data:
                v_id_str = str(v['_id'])
                v['id'] = v_id_str
                del v['_id']
                
                # Serializar fechas del vehículo para evitar error 500 en JSON
                if 'sucursal_id' in v:
                    v['sucursalId'] = v.pop('sucursal_id')
                if 'createdAt' in v and isinstance(v['createdAt'], datetime):
                    v['createdAt'] = v['createdAt'].isoformat()
                if 'updatedAt' in v and isinstance(v['updatedAt'], datetime):
                    v['updatedAt'] = v['updatedAt'].isoformat()
                
                vehiculos_map[v_id_str] = v

        ordenes = []
        for o in ordenes_list:
            o['id'] = str(o['_id'])
            del o['_id']
            
            # Enriquecer con datos frescos del vehículo
            v_id_str = o.get('vehiculo_id')
            if v_id_str in vehiculos_map:
                o['vehiculo_snapshot'] = vehiculos_map[v_id_str]
            
            # Serializar fechas dentro del snapshot si existen (por seguridad)
            vs = o.get('vehiculo_snapshot')
            if vs and isinstance(vs, dict):
                if 'sucursal_id' in vs:
                    vs['sucursalId'] = vs.pop('sucursal_id')
                if 'createdAt' in vs and isinstance(vs['createdAt'], datetime):
                    vs['createdAt'] = vs['createdAt'].isoformat()
                if 'updatedAt' in vs and isinstance(vs['updatedAt'], datetime):
                    vs['updatedAt'] = vs['updatedAt'].isoformat()
            
            if 'sucursal_id' in o:
                o['sucursalId'] = o.pop('sucursal_id')

            # Serializar fechas
            if 'createdAt' in o and isinstance(o['createdAt'], datetime):
                o['createdAt'] = o['createdAt'].isoformat()
            if 'updatedAt' in o and isinstance(o['updatedAt'], datetime):
                o['updatedAt'] = o['updatedAt'].isoformat()

            # Serializar evidencias si existen
            if 'evidencia' in o and isinstance(o['evidencia'], list):
                for ev in o['evidencia']:
                    if 'createdAt' in ev and isinstance(ev['createdAt'], datetime):
                        ev['createdAt'] = ev['createdAt'].isoformat()

            ordenes.append(o)
            
        response_data = {
            "items": ordenes,
            "total": total,
            "page": page,
            "limit": limit,
            "totalPages": (total + limit - 1) // limit
        }
        
        return create_response(200, "Órdenes recuperadas", response_data)
    except Exception as e:
        return handle_exception(e)
@logger.inject_lambda_context
def get_orden_handler(event, context):
    """GET /ordenes/{id} — Detalle de orden con vehículo enriquecido."""
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        orden_id = event['pathParameters']['id']
        db = get_tenant_db(tenant_id)

        orden = db["ordenes_servicio"].find_one({"_id": ObjectId(orden_id)})
        if not orden:
            return create_response(404, "Orden no encontrada.")
        
        if 'sucursal_id' in orden:
            orden['sucursalId'] = orden.pop('sucursal_id')

        orden['id'] = str(orden.pop('_id'))

        v_id = orden.get('vehiculo_id')
        if v_id and isinstance(v_id, str) and len(v_id) == 24:
            try:
                vehiculo = db["vehiculos"].find_one({"_id": ObjectId(v_id)})
                if vehiculo:
                    vehiculo['id'] = str(vehiculo.pop('_id'))
                    if 'sucursal_id' in vehiculo:
                        vehiculo['sucursalId'] = vehiculo.pop('sucursal_id')
                    if 'createdAt' in vehiculo and isinstance(vehiculo['createdAt'], datetime):
                        vehiculo['createdAt'] = vehiculo['createdAt'].isoformat()
                    if 'updatedAt' in vehiculo and isinstance(vehiculo['updatedAt'], datetime):
                        vehiculo['updatedAt'] = vehiculo['updatedAt'].isoformat()
                    orden['vehiculo_snapshot'] = vehiculo
            except Exception:
                pass

        for f in ('createdAt', 'updatedAt'):
            if f in orden and isinstance(orden[f], datetime):
                orden[f] = orden[f].isoformat()

        # Serializar evidencias si existen
        if 'evidencia' in orden and isinstance(orden['evidencia'], list):
            for ev in orden['evidencia']:
                if 'createdAt' in ev and isinstance(ev['createdAt'], datetime):
                    ev['createdAt'] = ev['createdAt'].isoformat()

        return create_response(200, "Orden obtenida", orden)
    except Exception as e:
        return handle_exception(e)


@logger.inject_lambda_context
def update_orden_handler(event, context):
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        orden_id = event['pathParameters']['id']
        body = json.loads(event.get('body', '{}'))
        
        db = get_tenant_db(tenant_id)
        
        # 1. Obtener la orden actual para comparar el estado
        orden_actual = db["ordenes_servicio"].find_one({"_id": ObjectId(orden_id)})
        if not orden_actual:
            return create_response(404, "Orden no encontrada")
            
        update_data = {}
        # Campos que el cliente puede pisar vía $set. 'total' se calcula server-side
        # (ver más abajo) y 'bitacora_estados' nunca se acepta para $set para no perder
        # el histórico — se maneja con $push.
        campos_permitidos = [
            'estado', 'motivo_cancelacion', 'puntosArreglar',
            'mecanico_id', 'mecanico_nombre', 'falla_reportada', 'diagnostico',
            'kilometraje', 'nivel_tanque', 'testigos_encendidos', 'inventario',
            'proximo_cambio_bujias', 'proximo_cambio_aceite', 'anticipo',
            'cliente_snapshot', 'vehiculo_snapshot',
            'aplica_costo_revision', 'costo_revision', 'fechaEstimadaEntrega',
            'cita_id', 'sucursal_id'
        ]

        # Mapear sucursalId a sucursal_id
        if 'sucursalId' in body:
            body['sucursal_id'] = body.pop('sucursalId')

        for campo in campos_permitidos:
            if campo in body:
                update_data[campo] = body[campo]

        # Recalcular total server-side desde puntosArreglar (excluyendo no_cobrar)
        # para evitar que el frontend mande totales manipulados que desincronicen
        # reportes vs venta real.
        puntos_para_total = update_data.get('puntosArreglar', orden_actual.get('puntosArreglar', []))
        update_data['total'] = _calcular_total_orden(puntos_para_total)

        # 2. Si el estado cambió, agregar a la bitácora automáticamente con $push.
        nuevo_estado = body.get('estado')
        bitacora_push = None
        if nuevo_estado and nuevo_estado != orden_actual.get('estado'):
            responsable = claims.get('email') or claims.get('name') or claims.get('sub') or 'system'
            bitacora_push = {
                "estado": nuevo_estado,
                "fecha": datetime.utcnow().isoformat() + "Z",
                "usuario_id": responsable
            }

        update_data['updatedAt'] = datetime.utcnow()

        update_doc = {"$set": update_data}
        if bitacora_push:
            update_doc["$push"] = {"bitacora_estados": bitacora_push}
        db["ordenes_servicio"].update_one({"_id": ObjectId(orden_id)}, update_doc)
        
        # Recuperar actualizada
        orden = db["ordenes_servicio"].find_one({"_id": ObjectId(orden_id)})
        orden['id'] = str(orden['_id'])
        del orden['_id']

        # Serializar fechas para JSON
        if 'createdAt' in orden and isinstance(orden['createdAt'], datetime):
            orden['createdAt'] = orden['createdAt'].isoformat()
        if 'updatedAt' in orden and isinstance(orden['updatedAt'], datetime):
            orden['updatedAt'] = orden['updatedAt'].isoformat()
            
        if 'sucursal_id' in orden:
            orden['sucursalId'] = orden.pop('sucursal_id')
            
        vs = orden.get('vehiculo_snapshot')
        if vs and isinstance(vs, dict):
            if 'sucursal_id' in vs:
                vs['sucursalId'] = vs.pop('sucursal_id')
            if 'createdAt' in vs and isinstance(vs['createdAt'], datetime):
                vs['createdAt'] = vs['createdAt'].isoformat()
            if 'updatedAt' in vs and isinstance(vs['updatedAt'], datetime):
                vs['updatedAt'] = vs['updatedAt'].isoformat()
        
        return create_response(200, "Orden actualizada", orden)
    except Exception as e:
        return handle_exception(e)
