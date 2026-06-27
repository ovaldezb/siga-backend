import json
import re
from datetime import datetime
from bson import ObjectId
from aws_lambda_powertools import Logger
from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.utils.auth_utils import parse_object_id, get_tenant_id, try_parse_id, resolve_sucursal_scope, get_claims, is_admin
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

VALID_ESTADOS = {"pendiente", "confirmada", "en_proceso", "completada", "cancelada", "pospuesta", "no_asistio"}


def _serialize(doc):
    doc['id'] = str(doc.pop('_id'))
    return doc


# Mapa: estado terminal de la OS -> estado que debe tomar la cita ligada.
_OS_ESTADO_A_CITA = {
    "FINALIZADO": "completada",
    "ENTREGADO": "completada",
    "CANCELADO": "cancelada",
}


def sync_cita_estado_por_orden(db, orden_id, os_estado, session=None):
    """Sincroniza el estado de la cita ligada a una OS cuando la OS llega a un estado
    terminal. FINALIZADO/ENTREGADO -> 'completada'; CANCELADO -> 'cancelada'.

    Antes la cita quedaba clavada en 'en_proceso' aunque su OS ya se hubiera
    finalizado/cobrado (el tab de Citas la seguía mostrando "En Proceso"). Esta
    función la llaman los flujos que cierran una OS (POS, abono, cambio manual).

    Best-effort: no pisa estados terminales de la cita y nunca rompe el flujo
    llamador (las excepciones se registran y se ignoran).
    """
    if not orden_id:
        return
    nuevo = _OS_ESTADO_A_CITA.get(os_estado)
    if not nuevo:
        return
    try:
        db["citas"].update_one(
            {"orden_id": str(orden_id), "estado": {"$nin": ["completada", "cancelada"]}},
            {"$set": {"estado": nuevo, "updatedAt": iso_utc()}},
            session=session,
        )
    except Exception as e:
        logger.warning(f"No se pudo sincronizar estado de cita para OS {orden_id}: {e}")


def sync_citas_estados_handler(event, context):
    """POST /citas/sync-estados — Backoffice ADMIN: reconcilia el estado de las citas
    cuyo OS ligado ya quedó terminal pero la cita siguió en 'en_proceso' (u otro
    estado activo). Arregla el backlog histórico de un solo golpe.
    """
    try:
        claims = get_claims(event)
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")
        if not is_admin(claims):
            return create_response(403, "Solo un administrador puede sincronizar el estado de las citas.")

        db = get_tenant_db(tenant_id)

        # Citas activas (no terminales) que tienen una OS ligada.
        citas = list(db.citas.find(
            {"orden_id": {"$nin": [None, ""]}, "estado": {"$nin": ["completada", "cancelada"]}},
            {"orden_id": 1, "estado": 1}
        ))

        # Resolver el estado actual de cada OS ligada en un solo query.
        oids = []
        for c in citas:
            try:
                oids.append(ObjectId(c['orden_id']))
            except Exception:
                pass
        os_map = {}
        if oids:
            for o in db.ordenes_servicio.find({"_id": {"$in": oids}}, {"estado": 1}):
                os_map[str(o['_id'])] = o.get('estado')

        actualizadas = 0
        for c in citas:
            os_estado = os_map.get(str(c.get('orden_id')))
            nuevo = _OS_ESTADO_A_CITA.get(os_estado)
            if nuevo and nuevo != c.get('estado'):
                db.citas.update_one(
                    {"_id": c['_id']},
                    {"$set": {"estado": nuevo, "updatedAt": iso_utc()}}
                )
                actualizadas += 1

        return create_response(200, f"{actualizadas} cita(s) sincronizada(s).", {
            "actualizadas": actualizadas,
            "revisadas": len(citas),
        })
    except Exception as e:
        return handle_exception(e)


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
        # Filtros adicionales para tabs (activas/canceladas) sin tocar el dropdown del front
        estado_in = (query_params.get('estado_in') or '').strip()
        estado_ne = (query_params.get('estado_ne') or '').strip()
        if estado_in:
            estados_list = [e.strip() for e in estado_in.split(',') if e.strip()]
            if estados_list:
                filter_query["estado"] = {"$in": estados_list}
        if estado_ne:
            estados_ne_list = [e.strip() for e in estado_ne.split(',') if e.strip()]
            if estados_ne_list:
                # Si ya hay un $in, mantenerlo y añadir $nin como condición adicional
                if isinstance(filter_query.get("estado"), dict):
                    filter_query["estado"]["$nin"] = estados_ne_list
                else:
                    filter_query["estado"] = {"$nin": estados_ne_list}
        if fecha_desde or fecha_hasta:
            filter_query["fecha"] = {}
            if fecha_desde:
                filter_query["fecha"]["$gte"] = fecha_desde
            if fecha_hasta:
                filter_query["fecha"]["$lte"] = fecha_hasta

        total = db.citas.count_documents(filter_query)
        # Orden: fecha y hora descendente (las últimas primero), con createdAt
        # como desempate para citas registradas el mismo día/hora.
        citas = list(
            db.citas.find(filter_query)
            .sort([("fecha", -1), ("horaInicio", -1), ("createdAt", -1)])
            .skip(skip)
            .limit(limit)
        )
        citas = [_serialize(c) for c in citas]

        # ENRIQUECIMIENTO REACTIVO DE METADATOS DE CLIENTE (ÓRDENES, COTIZACIONES, VEHÍCULOS)
        cliente_ids = list({c['clienteId'] for c in citas if c.get('clienteId')})
        veh_counts = {}
        pending_os_counts = {}
        pending_cot_counts = {}
        cliente_phones = {}

        if cliente_ids:
            # Teléfono del cliente para el botón WhatsApp en la fila.
            cliente_object_ids = []
            for cid in cliente_ids:
                try:
                    cliente_object_ids.append(ObjectId(cid))
                except Exception:
                    pass
            if cliente_object_ids:
                cli_docs = db.clientes.find(
                    {"_id": {"$in": cliente_object_ids}},
                    {"telefono": 1}
                )
                cliente_phones = {str(d["_id"]): (d.get("telefono") or "") for d in cli_docs}
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
            c['cliente_telefono'] = cliente_phones.get(cid, '') if cid else ''

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

        # La cita NO genera Orden de Servicio automáticamente. La OS se crea
        # sólo cuando el usuario decide convertir la cita (cliente presente),
        # desde el flujo manual en sae-app -> ordenes_manager.create con cita_id.
        return create_response(201, "Cita creada exitosamente", nueva)
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
