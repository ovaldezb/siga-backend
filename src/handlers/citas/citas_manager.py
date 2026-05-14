import json
import re
from datetime import datetime
from bson import ObjectId
from aws_lambda_powertools import Logger
from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.utils.auth_utils import parse_object_id, get_tenant_id, try_parse_id
from src.shared.infrastructure.database import get_tenant_db
from pymongo import ReturnDocument

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
        if sucursal_id:
            and_conditions.append({'sucursal_id': sucursal_id})
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

        nueva = {
            "clienteId": body.get('clienteId'),
            "clienteNombre": body.get('clienteNombre'),
            "vehiculoId": body.get('vehiculoId'),
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
            "createdAt": datetime.utcnow().isoformat(),
            "updatedAt": datetime.utcnow().isoformat(),
            "tenant_id": tenant_id,
            "sucursal_id": body.get('sucursal_id')
        }

        result = db.citas.insert_one(nueva)
        cita_id = str(result.inserted_id)
        nueva['id'] = cita_id
        del nueva['_id']

        # NUEVO: Crear Orden de Servicio automáticamente
        try:
            # 1. Obtener siguiente folio de OS scoped por sucursal (consistente con ventas/folios_manager)
            from src.handlers.admin.folios_manager import _get_next_folio_internal
            sucursal_id_cita = body.get("sucursal_id")
            if not sucursal_id_cita:
                raise ValueError("La cita no tiene sucursal_id, no se puede crear folio de OS")
            folio = _get_next_folio_internal(tenant_id, "os", sucursal_id_cita)

            # 2. Crear snapshot del cliente
            cliente_id = body.get('clienteId')
            cliente_doc = db.clientes.find_one({"_id": ObjectId(cliente_id)}) if cliente_id else None
            cliente_snapshot = {}
            if cliente_doc:
                cliente_snapshot = {
                    "id": str(cliente_doc["_id"]),
                    "nombre": cliente_doc.get("nombre"),
                    "apellido_paterno": cliente_doc.get("apellido_paterno"),
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
                    "fecha": datetime.utcnow().isoformat() + "Z",
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

        except Exception as os_err:
            # No bloqueamos la creación de la cita si falla la OS automática, pero lo logueamos
            print(f"Error creando OS automática para cita {cita_id}: {os_err}")

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

        update_doc['updatedAt'] = datetime.utcnow().isoformat()

        db = get_tenant_db(tenant_id)
        result = db.citas.find_one_and_update(
            {"_id": object_id},
            {"$set": update_doc},
            return_document=ReturnDocument.AFTER
        )

        if not result:
            return create_response(404, "Cita no encontrada.")

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
        result = db.citas.delete_one({"_id": object_id})

        if result.deleted_count == 0:
            return create_response(404, "Cita no encontrada.")

        return create_response(200, "Cita eliminada")
    except Exception as e:
        return handle_exception(e)
