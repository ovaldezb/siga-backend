import json
import re
from datetime import datetime
from bson import ObjectId
from aws_lambda_powertools import Logger
from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.utils.auth_utils import parse_object_id, get_tenant_id, try_parse_id, resolve_sucursal_scope, get_claims
from src.shared.infrastructure.database import get_tenant_db
from src.shared.utils.os_events import (
    append_os_event,
    OS_EVENT_CREATED,
    OS_EVENT_ESTADO_CHANGED,
)
from pymongo import ReturnDocument
from src.shared.utils.date_utils import iso_utc

logger = Logger()

ALLOWED_FIELDS = {
    "clienteId", "clienteNombre", "vehiculoId", "vehiculoDesc",
    "tecnicoId", "tecnicoNombre", "fecha", "horaInicio", "horaFin",
    "servicio", "estado", "notas", "orden_id"
}

VALID_ESTADOS = {"pendiente", "confirmada", "en_proceso", "completada", "cancelada"}


def _serialize(doc):
    doc['id'] = str(doc.pop('_id'))
    return doc


def list_citas_handler(event, context):
    try:
        tenant_id = get_tenant_id(event)
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        query_params = event.get('queryStringParameters') or {}
        search_query = (query_params.get('q') or '').strip()
        estado = (query_params.get('estado') or '').strip()
        fecha_desde = (query_params.get('fecha_desde') or '').strip()
        fecha_hasta = (query_params.get('fecha_hasta') or '').strip()
        page = int(query_params.get('page', 1))
        limit = int(query_params.get('limit', 100))
        skip = (page - 1) * limit

        db = get_tenant_db(tenant_id)
        filter_query = {}
        and_conditions = []

        sucursal_id = query_params.get('sucursal_id')

        # Enforce scope contra las sucursales permitidas del usuario
        scope_list, scope_err = resolve_sucursal_scope(get_claims(event), db, sucursal_id)
        if scope_err:
            return create_response(403, scope_err)
        if scope_list is not None:
            if len(scope_list) == 1:
                and_conditions.append({'sucursal_id': scope_list[0]})
            else:
                and_conditions.append({'sucursal_id': {'$in': scope_list}})
        if search_query:
            regex = re.compile(re.escape(search_query), re.IGNORECASE)
            and_conditions.append({'$or': [
                {"clienteNombre": regex},
                {"servicio": regex},
                {"tecnicoNombre": regex}
            ]})
        
        if and_conditions:
            filter_query['$and'] = and_conditions
            
        if estado and estado != 'todos':
            filter_query["estado"] = estado
        if fecha_desde or fecha_hasta:
            filter_query["fecha"] = {}
            if fecha_desde:
                filter_query["fecha"]["$gte"] = fecha_desde
            if fecha_hasta:
                filter_query["fecha"]["$lte"] = fecha_hasta

        total = db.citas.count_documents(filter_query)
        citas = list(
            db.citas.find(filter_query)
            .sort([("fecha", 1), ("horaInicio", 1)])
            .skip(skip)
            .limit(limit)
        )
        citas = [_serialize(c) for c in citas]

        # ENRIQUECIMIENTO REACTIVO DE METADATOS DE CLIENTE (ÓRDENES, COTIZACIONES, VEHÍCULOS)
        cliente_ids = list({c['clienteId'] for c in citas if c.get('clienteId')})
        veh_counts = {}
        pending_os_counts = {}
        pending_cot_counts = {}

        if cliente_ids:
            # Conteo de vehículos por cliente
            counts_agg = list(db["vehiculos"].aggregate([
                {"$match": {"cliente_id": {"$in": cliente_ids}}},
                {"$group": {"_id": "$cliente_id", "count": {"$sum": 1}}}
            ]))
            veh_counts = {item['_id']: item['count'] for item in counts_agg}
            
            # Conteo de Órdenes de Servicio activas en taller (APROBADO, EN_PROCESO)
            os_agg = list(db["ordenes_servicio"].aggregate([
                {"$match": {
                    "cliente_snapshot.id": {"$in": cliente_ids},
                    "estado": {"$in": ["APROBADO", "EN_PROCESO"]}
                }},
                {"$group": {"_id": "$cliente_snapshot.id", "count": {"$sum": 1}}}
            ]))
            pending_os_counts = {item['_id']: item['count'] for item in os_agg}
            
            # Conteo de Cotizaciones pendientes por aprobar (RECEPCION, COTIZADO)
            cot_agg = list(db["ordenes_servicio"].aggregate([
                {"$match": {
                    "cliente_snapshot.id": {"$in": cliente_ids},
                    "estado": {"$in": ["RECEPCION", "COTIZADO"]}
                }},
                {"$group": {"_id": "$cliente_snapshot.id", "count": {"$sum": 1}}}
            ]))
            pending_cot_counts = {item['_id']: item['count'] for item in cot_agg}

        for c in citas:
            cid = c.get('clienteId')
            c['num_vehiculos'] = veh_counts.get(cid, 1) if cid else 1
            c['os_pendientes'] = pending_os_counts.get(cid, 0) if cid else 0
            c['cotizaciones_pendientes'] = pending_cot_counts.get(cid, 0) if cid else 0

        return create_response(200, "Citas obtenidas", {
            "items": citas,
            "total": total,
            "page": page,
            "limit": limit
        })
    except Exception as e:
        return handle_exception(e)


def create_cita_handler(event, context):
    try:
        tenant_id = get_tenant_id(event)
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        body = json.loads(event.get('body', '{}'))

        if not body.get('servicio'):
            return create_response(400, "El campo 'servicio' es requerido.")
        if not body.get('fecha'):
            return create_response(400, "El campo 'fecha' es requerido.")
        if not body.get('horaInicio'):
            return create_response(400, "El campo 'horaInicio' es requerido.")

        estado = body.get('estado') or 'pendiente'
        if estado not in VALID_ESTADOS:
            estado = 'pendiente' # Fallback seguro

        db = get_tenant_db(tenant_id)

        # CONSOLIDACIÓN ROBUSTA DE CLIENTE Y VEHÍCULO
        cliente_id = body.get('clienteId')
        cliente_nombre = body.get('clienteNombre')
        vehiculo_id = body.get('vehiculoId')
        vehiculo_desc = body.get('vehiculoDesc')

        # 1. Verificar/Crear Cliente en Colección de Clientes
        if cliente_id:
            try:
                cli_doc = db.clientes.find_one({"_id": ObjectId(cliente_id)})
                if not cli_doc:
                    cliente_id = None
            except Exception:
                cliente_id = None

        if cliente_nombre and not cliente_id:
            import re
            nombre_clean = cliente_nombre.strip()
            regex = re.compile(f"^{re.escape(nombre_clean)}$", re.IGNORECASE)
            cliente_existente = db.clientes.find_one({"nombre": regex})
            if cliente_existente:
                cliente_id = str(cliente_existente["_id"])
                body['clienteId'] = cliente_id
                body['clienteNombre'] = f"{cliente_existente.get('nombre')} {cliente_existente.get('apellido_paterno', '')}".strip()
            else:
                partes = nombre_clean.split(" ", 1)
                nombre = partes[0]
                apellido_paterno = partes[1] if len(partes) > 1 else "S/A"
                sucursal_id_c = body.get('sucursal_id')
                if not sucursal_id_c:
                    suc_doc = db.sucursales.find_one({})
                    sucursal_id_c = str(suc_doc["_id"]) if suc_doc else "default"

                nuevo_cliente = {
                    "nombre": nombre,
                    "apellido_paterno": apellido_paterno,
                    "apellido_materno": "",
                    "telefono": body.get("clienteTelefono", "") or "0000000000",
                    "email": body.get("clienteEmail", "") or "",
                    "rfc": "XAXX010101000",
                    "razon_social": "",
                    "regimen_fiscal": "612",
                    "codigo_postal": "",
                    "tipo_persona": "FISICA",
                    "limite_credito": 0.0,
                    "dias_credito": 0,
                    "nivel_precio": 1,
                    "vehiculos_resumen": [],
                    "sucursal_id": sucursal_id_c,
                    "flotilla_id": None,
                    "createdAt": iso_utc(),
                    "tenant_id": tenant_id
                }
                res_cli = db.clientes.insert_one(nuevo_cliente)
                cliente_id = str(res_cli.inserted_id)
                body['clienteId'] = cliente_id
                body['clienteNombre'] = f"{nombre} {apellido_paterno}".strip()

        # 2. Verificar/Crear Vehículo en Colección de Vehículos
        if vehiculo_id:
            try:
                veh_doc = db.vehiculos.find_one({"_id": ObjectId(vehiculo_id)})
                if not veh_doc:
                    vehiculo_id = None
            except Exception:
                vehiculo_id = None

        if not vehiculo_id and vehiculo_desc and cliente_id:
            import re
            placas = "S/P"
            marca = "Genérico"
            modelo = "Vehículo"
            
            match_placas = re.search(r'\((.*?)\)', vehiculo_desc)
            if match_placas:
                placas = match_placas.group(1).strip()
                desc_sin_placas = vehiculo_desc.replace(f"({placas})", "").strip()
            else:
                desc_sin_placas = vehiculo_desc.strip()
                
            partes_v = desc_sin_placas.split(" ", 1)
            if partes_v:
                marca = partes_v[0]
                if len(partes_v) > 1:
                    modelo = partes_v[1]
            
            veh_existente = db.vehiculos.find_one({"placas": placas}) if placas != "S/P" else None
            if veh_existente:
                vehiculo_id = str(veh_existente["_id"])
                body['vehiculoId'] = vehiculo_id
                body['vehiculoDesc'] = f"{veh_existente.get('marca')} {veh_existente.get('modelo')} ({veh_existente.get('placas')})".strip()
            else:
                sucursal_id_v = body.get('sucursal_id')
                if not sucursal_id_v:
                    suc_doc = db.sucursales.find_one({})
                    sucursal_id_v = str(suc_doc["_id"]) if suc_doc else "default"
                    
                nuevo_vehiculo = {
                    "marca": marca,
                    "modelo": modelo,
                    "placas": placas,
                    "cliente_id": cliente_id,
                    "sucursal_id": sucursal_id_v,
                    "tenant_id": tenant_id,
                    "createdAt": datetime.utcnow()
                }
                res_veh = db.vehiculos.insert_one(nuevo_vehiculo)
                vehiculo_id = str(res_veh.inserted_id)
                body['vehiculoId'] = vehiculo_id
                body['vehiculoDesc'] = f"{marca} {modelo} ({placas})".strip()
                
                vehiculo_resumen = {
                    "id": vehiculo_id,
                    "placas": placas,
                    "marca": marca,
                    "modelo": modelo,
                    "anio": ""
                }
                db.clientes.update_one(
                    {"_id": ObjectId(cliente_id)},
                    {"$push": {"vehiculos_resumen": vehiculo_resumen}}
                )

        nueva = {
            "clienteId": cliente_id,
            "clienteNombre": body.get('clienteNombre'),
            "vehiculoId": vehiculo_id,
            "vehiculoDesc": body.get('vehiculoDesc'),
            "tecnicoId": body.get('tecnicoId'),
            "tecnicoNombre": body.get('tecnicoNombre'),
            "fecha": body.get('fecha'),
            "horaInicio": body.get('horaInicio'),
            "horaFin": body.get('horaFin'),
            "servicio": body.get('servicio'),
            "estado": estado,
            "notas": body.get('notas'),
            "orden_id": body.get('orden_id'),
            "createdAt": iso_utc(),
            "updatedAt": iso_utc(),
            "tenant_id": tenant_id,
            "sucursal_id": body.get('sucursal_id')
        }

        result = db.citas.insert_one(nueva)
        cita_id = str(result.inserted_id)
        nueva['id'] = cita_id
        del nueva['_id']

        # NUEVO: Crear Orden de Servicio automáticamente — sólo si la cita
        # no nace cancelada (no tendría sentido abrir OS para una cita cancelada).
        if estado == 'cancelada':
            return create_response(201, "Cita creada (sin OS por estado=cancelada)", nueva)

        try:
            # 1. Obtener siguiente folio de OS scoped por sucursal (consistente con ventas/folios_manager)
            from src.handlers.admin.folios_manager import _get_next_folio_internal
            sucursal_id_cita = body.get("sucursal_id")
            if not sucursal_id_cita:
                raise ValueError("La cita no tiene sucursal_id, no se puede crear folio de OS")
            folio = _get_next_folio_internal(tenant_id, "os", sucursal_id_cita)

            # 2. Crear snapshot del cliente (incluye apellido_materno para PDF Orden y Cotización)
            cliente_id = body.get('clienteId')
            cliente_doc = None
            if cliente_id:
                try:
                    cliente_doc = db.clientes.find_one({"_id": ObjectId(cliente_id)})
                except Exception:
                    cliente_doc = None
            cliente_snapshot = {}
            if cliente_doc:
                cliente_snapshot = {
                    "id": str(cliente_doc["_id"]),
                    "nombre": cliente_doc.get("nombre"),
                    "apellido_paterno": cliente_doc.get("apellido_paterno"),
                    "apellido_materno": cliente_doc.get("apellido_materno"),
                    "telefono": cliente_doc.get("telefono"),
                    "email": cliente_doc.get("email")
                }
            else:
                cliente_snapshot = {"nombre": body.get("clienteNombre"), "id": cliente_id}

            # 3. Crear documento de OS
            responsable = body.get('usuario_email') or 'system'
            os_doc = {
                "folio": folio,
                "tenant_id": tenant_id,
                "sucursal_id": body.get("sucursal_id"),
                "estado": "RECEPCION",
                "bitacora_estados": [{
                    "estado": "RECEPCION",
                    "fecha": iso_utc(),
                    "usuario_id": responsable
                }],
                "cliente_snapshot": cliente_snapshot,
                "vehiculo_id": body.get("vehiculoId"),
                "cita_id": cita_id,
                "puntosArreglar": [{
                    "nombre": body.get("servicio", "Servicio General"),
                    "items": []
                }],
                "falla_reportada": body.get("notas", ""),
                "mecanico_id": body.get("tecnicoId"),
                "mecanico_nombre": body.get("tecnicoNombre"),
                "total": 0,
                "anticipo": 0,
                "createdAt": datetime.utcnow(),
                "updatedAt": datetime.utcnow()
            }

            os_result = db.ordenes_servicio.insert_one(os_doc)
            orden_id = str(os_result.inserted_id)

            # Actualizar la cita con el orden_id
            db.citas.update_one({"_id": ObjectId(cita_id)}, {"$set": {"orden_id": orden_id}})
            nueva['orden_id'] = orden_id

            # Audit log (item #16) — OS creada vía flujo cita.
            append_os_event(
                db, tenant_id, orden_id, OS_EVENT_CREATED,
                payload={"folio": folio, "estado": "RECEPCION", "cita_id": cita_id},
                claims=get_claims(event), event=event,
            )

        except Exception as os_err:
            # No bloqueamos la creación de la cita si falla la OS automática, pero lo logueamos
            logger.warning(f"Error creando OS automática para cita {cita_id}: {os_err}")

        return create_response(201, "Cita y Orden de Servicio creadas exitosamente", nueva)
    except Exception as e:
        return handle_exception(e)


def get_cita_handler(event, context):
    try:
        tenant_id = get_tenant_id(event)
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        cita_id = event['pathParameters']['id']
        object_id, err = parse_object_id(cita_id)
        if err:
            return create_response(400, err)

        db = get_tenant_db(tenant_id)
        cita = db.citas.find_one({"_id": object_id})

        if not cita:
            return create_response(404, "Cita no encontrada.")

        return create_response(200, "Detalle de la cita", _serialize(cita))
    except Exception as e:
        return handle_exception(e)


def update_cita_handler(event, context):
    try:
        tenant_id = get_tenant_id(event)
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        cita_id = event['pathParameters']['id']
        object_id, err = parse_object_id(cita_id)
        if err:
            return create_response(400, err)

        body = json.loads(event.get('body', '{}'))
        update_doc = {k: body[k] for k in ALLOWED_FIELDS if k in body}

        if not update_doc:
            return create_response(400, "No hay campos válidos para actualizar.")

        if 'estado' in update_doc and update_doc['estado'] not in VALID_ESTADOS:
            return create_response(400, f"Estado inválido. Use: {', '.join(sorted(VALID_ESTADOS))}.")

        update_doc['updatedAt'] = iso_utc()

        db = get_tenant_db(tenant_id)
        result = db.citas.find_one_and_update(
            {"_id": object_id},
            {"$set": update_doc},
            return_document=ReturnDocument.AFTER
        )

        if not result:
            return create_response(404, "Cita no encontrada.")

        # Si la cita se canceló, cancelar la OS ligada si todavía está en RECEPCIÓN
        # (no tocar OS que ya avanzaron a COTIZADO/APROBADO/etc — esas representan trabajo real)
        if update_doc.get('estado') == 'cancelada' and result.get('orden_id'):
            try:
                upd = db.ordenes_servicio.update_one(
                    {"_id": ObjectId(result['orden_id']), "estado": "RECEPCION"},
                    {"$set": {
                        "estado": "CANCELADO",
                        "motivo_cancelacion": "Cita cancelada",
                        "updatedAt": datetime.utcnow()
                    }, "$push": {"bitacora_estados": {
                        "estado": "CANCELADO",
                        "fecha": iso_utc(),
                        "usuario_id": "system:cita_cancelada"
                    }}}
                )
                # Audit log (item #16) — solo si efectivamente cambió el estado.
                if upd.modified_count > 0:
                    append_os_event(
                        db, tenant_id, result['orden_id'], OS_EVENT_ESTADO_CHANGED,
                        payload={"from": "RECEPCION", "to": "CANCELADO", "motivo": "Cita cancelada"},
                        claims={"email": "system:cita_cancelada"}, event=event,
                    )
            except Exception as os_err:
                logger.warning(f"No se pudo cancelar OS ligada {result.get('orden_id')}: {os_err}")

        return create_response(200, "Cita actualizada", _serialize(result))
    except Exception as e:
        return handle_exception(e)


def delete_cita_handler(event, context):
    try:
        tenant_id = get_tenant_id(event)
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        cita_id = event['pathParameters']['id']
        object_id, err = parse_object_id(cita_id)
        if err:
            return create_response(400, err)

        db = get_tenant_db(tenant_id)

        # Bloquear borrado si la OS ligada ya avanzó (datos contables/operativos asociados)
        cita = db.citas.find_one({"_id": object_id})
        if cita and cita.get('orden_id'):
            try:
                os_doc = db.ordenes_servicio.find_one({"_id": ObjectId(cita['orden_id'])})
            except Exception:
                os_doc = None
            if os_doc and os_doc.get('estado') not in ('RECEPCION', 'CANCELADO'):
                return create_response(409,
                    f"No se puede eliminar la cita: la OS {os_doc.get('folio')} ya está en estado {os_doc.get('estado')}. Cancele la cita en su lugar.")
            # OS aún en RECEPCION o ya CANCELADA — limpiar referencia o eliminar la OS huérfana en RECEPCION
            if os_doc and os_doc.get('estado') == 'RECEPCION':
                try:
                    db.ordenes_servicio.delete_one({"_id": os_doc['_id']})
                except Exception as os_err:
                    logger.warning(f"No se pudo eliminar OS ligada en RECEPCION {cita.get('orden_id')}: {os_err}")

        result = db.citas.delete_one({"_id": object_id})

        if result.deleted_count == 0:
            return create_response(404, "Cita no encontrada.")

        return create_response(200, "Cita eliminada")
    except Exception as e:
        return handle_exception(e)
