"""
Cotizaciones independientes (no atadas a una Orden de Servicio).

Caso de uso típico: empresas/flotillas piden cotización formal antes de llevar
el auto. Cuando el cliente acepta y trae el vehículo, se convierte la cotización
en una OS preservando cliente, vehículo, puntos e items.

Schema (colección `cotizaciones`):
    _id, tenant_id, sucursal_id, folio (COT-YYYYMMDD-####),
    status: BORRADOR | ENVIADA | ACEPTADA | RECHAZADA | CONVERTIDA | EXPIRADA,
    cliente_snapshot, vehiculo_snapshot, puntosArreglar,
    subtotal, iva, total,
    observaciones, vigencia_dias, vigencia_hasta,
    cotizacion_origen_id  (cuando se clonó),
    os_destino_id          (cuando se convirtió),
    createdAt, updatedAt, created_by
"""
import json
from datetime import datetime, timedelta
from bson import ObjectId
from bson.errors import InvalidId
from pymongo import ReturnDocument
from aws_lambda_powertools import Logger

from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.infrastructure.database import get_tenant_db
from src.shared.utils.auth_utils import (
    get_claims,
    get_tenant_id,
    parse_object_id,
    resolve_sucursal_scope,
)
from src.shared.utils.date_utils import iso_utc
from src.handlers.ordenes.ordenes_manager import _calcular_totales_orden

logger = Logger()

ALLOWED_STATUS = {
    "BORRADOR", "ENVIADA", "ACEPTADA", "RECHAZADA", "CONVERTIDA", "EXPIRADA",
}


def _next_folio_cotizacion(db, sucursal_id: str) -> str:
    """Folio atómico COT-YYYYMMDD-#### scoped por sucursal."""
    res = db.folios.find_one_and_update(
        {"tipo": "cot", "sucursal_id": sucursal_id},
        {"$inc": {"secuencia": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    seq = res.get("secuencia", 1)
    date_str = datetime.utcnow().strftime("%Y%m%d")
    return f"COT-{date_str}-{str(seq).zfill(4)}"


def _ensure_folio_index(db) -> None:
    try:
        db["cotizaciones"].create_index(
            [("folio", 1)],
            unique=True,
            partialFilterExpression={"folio": {"$exists": True, "$type": "string"}},
            name="uniq_cot_folio",
        )
    except Exception as idx_err:
        logger.warning(f"No se pudo verificar índice único de folio cotización: {idx_err}")


def _serialize(doc: dict) -> dict:
    if not doc:
        return doc
    doc = dict(doc)
    if "_id" in doc:
        doc["id"] = str(doc.pop("_id"))
    for k in ("createdAt", "updatedAt", "vigencia_hasta"):
        if isinstance(doc.get(k), datetime):
            doc[k] = iso_utc(doc[k])
    return doc


def _vigencia_hasta(created_at: datetime, dias: int) -> datetime:
    return created_at + timedelta(days=max(1, int(dias or 15)))


# ============================================================
# LIST
# ============================================================
@logger.inject_lambda_context
def list_cotizaciones_handler(event, context):
    try:
        claims = get_claims(event)
        tenant_id = claims.get("custom:tenant_id")
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        db = get_tenant_db(tenant_id)
        qs = event.get("queryStringParameters") or {}
        requested_sucursal = qs.get("sucursalId")
        sucursales_filter, scope_err = resolve_sucursal_scope(claims, db, requested_sucursal)
        if scope_err:
            return create_response(403, scope_err)

        query: dict = {"tenant_id": tenant_id}
        if sucursales_filter is not None:
            query["sucursal_id"] = {"$in": sucursales_filter} if len(sucursales_filter) > 1 else sucursales_filter[0]

        status = qs.get("status")
        if status:
            query["status"] = status.upper()

        cursor = db["cotizaciones"].find(query).sort("createdAt", -1).limit(int(qs.get("limit", 200)))
        cotizaciones = [_serialize(d) for d in cursor]
        return create_response(200, "Cotizaciones obtenidas", cotizaciones)
    except Exception as e:
        return handle_exception(e, event)


# ============================================================
# GET ONE
# ============================================================
@logger.inject_lambda_context
def get_cotizacion_handler(event, context):
    try:
        claims = get_claims(event)
        tenant_id = claims.get("custom:tenant_id")
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        cot_id = (event.get("pathParameters") or {}).get("id")
        oid, err = parse_object_id(cot_id)
        if err:
            return create_response(400, err)

        db = get_tenant_db(tenant_id)
        doc = db["cotizaciones"].find_one({"_id": oid, "tenant_id": tenant_id})
        if not doc:
            return create_response(404, "Cotización no encontrada")
        return create_response(200, "Cotización obtenida", _serialize(doc))
    except Exception as e:
        return handle_exception(e, event)


# ============================================================
# CREATE
# ============================================================
@logger.inject_lambda_context
def create_cotizacion_handler(event, context):
    try:
        claims = get_claims(event)
        tenant_id = claims.get("custom:tenant_id")
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        body = json.loads(event.get("body") or "{}")
        sucursal_id = body.get("sucursalId") or body.get("sucursal_id")
        if not sucursal_id:
            return create_response(400, "El campo 'sucursalId' es obligatorio.")

        db = get_tenant_db(tenant_id)

        # Validar scope de sucursal
        sucursales_filter, scope_err = resolve_sucursal_scope(claims, db, sucursal_id)
        if scope_err:
            return create_response(403, scope_err)

        _ensure_folio_index(db)
        folio = _next_folio_cotizacion(db, sucursal_id)

        cliente_snapshot = body.get("cliente_snapshot") or {}
        vehiculo_snapshot = body.get("vehiculo_snapshot") or {}
        puntos = body.get("puntosArreglar") or []

        if not cliente_snapshot or not (cliente_snapshot.get("nombre") or cliente_snapshot.get("razon_social")):
            return create_response(400, "El cliente (nombre o razón social) es obligatorio.")

        totales = _calcular_totales_orden(puntos)
        now = datetime.utcnow()
        vigencia_dias = int(body.get("vigencia_dias") or 15)

        doc = {
            "tenant_id": tenant_id,
            "sucursal_id": sucursal_id,
            "folio": folio,
            "status": (body.get("status") or "BORRADOR").upper(),
            "cliente_snapshot": cliente_snapshot,
            "vehiculo_snapshot": vehiculo_snapshot,
            "puntosArreglar": puntos,
            "subtotal": totales["subtotal"],
            "iva": totales["iva"],
            "total": totales["total"],
            "observaciones": body.get("observaciones", ""),
            "vigencia_dias": vigencia_dias,
            "vigencia_hasta": _vigencia_hasta(now, vigencia_dias),
            "cotizacion_origen_id": body.get("cotizacion_origen_id"),
            "os_destino_id": None,
            "kilometraje": body.get("kilometraje"),
            "createdAt": now,
            "updatedAt": now,
            "created_by": claims.get("email") or claims.get("sub"),
        }

        if doc["status"] not in ALLOWED_STATUS:
            return create_response(400, f"Status inválido. Permitidos: {sorted(ALLOWED_STATUS)}")

        res = db["cotizaciones"].insert_one(doc)
        doc["_id"] = res.inserted_id
        return create_response(201, "Cotización creada", _serialize(doc))
    except Exception as e:
        return handle_exception(e, event)


# ============================================================
# UPDATE
# ============================================================
@logger.inject_lambda_context
def update_cotizacion_handler(event, context):
    try:
        claims = get_claims(event)
        tenant_id = claims.get("custom:tenant_id")
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        cot_id = (event.get("pathParameters") or {}).get("id")
        oid, err = parse_object_id(cot_id)
        if err:
            return create_response(400, err)

        body = json.loads(event.get("body") or "{}")
        db = get_tenant_db(tenant_id)

        actual = db["cotizaciones"].find_one({"_id": oid, "tenant_id": tenant_id})
        if not actual:
            return create_response(404, "Cotización no encontrada")
        if actual.get("status") == "CONVERTIDA":
            return create_response(409, "La cotización ya fue convertida a OS y no se puede modificar.")

        # Campos editables (whitelist — folio y tenant_id no se tocan).
        editable = {}
        for fld in (
            "cliente_snapshot", "vehiculo_snapshot", "puntosArreglar",
            "observaciones", "vigencia_dias", "kilometraje",
        ):
            if fld in body:
                editable[fld] = body[fld]

        if "status" in body:
            new_status = (body["status"] or "").upper()
            if new_status not in ALLOWED_STATUS:
                return create_response(400, f"Status inválido. Permitidos: {sorted(ALLOWED_STATUS)}")
            editable["status"] = new_status

        if "puntosArreglar" in editable:
            totales = _calcular_totales_orden(editable["puntosArreglar"])
            editable.update(totales)

        if "vigencia_dias" in editable:
            base = actual.get("createdAt") or datetime.utcnow()
            editable["vigencia_hasta"] = _vigencia_hasta(base, editable["vigencia_dias"])

        editable["updatedAt"] = datetime.utcnow()

        updated = db["cotizaciones"].find_one_and_update(
            {"_id": oid, "tenant_id": tenant_id},
            {"$set": editable},
            return_document=ReturnDocument.AFTER,
        )
        return create_response(200, "Cotización actualizada", _serialize(updated))
    except Exception as e:
        return handle_exception(e, event)


# ============================================================
# DELETE
# ============================================================
@logger.inject_lambda_context
def delete_cotizacion_handler(event, context):
    try:
        claims = get_claims(event)
        tenant_id = claims.get("custom:tenant_id")
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        cot_id = (event.get("pathParameters") or {}).get("id")
        oid, err = parse_object_id(cot_id)
        if err:
            return create_response(400, err)

        db = get_tenant_db(tenant_id)
        actual = db["cotizaciones"].find_one({"_id": oid, "tenant_id": tenant_id}, {"status": 1})
        if not actual:
            return create_response(404, "Cotización no encontrada")
        if actual.get("status") == "CONVERTIDA":
            return create_response(409, "No se puede eliminar una cotización convertida a OS.")

        db["cotizaciones"].delete_one({"_id": oid, "tenant_id": tenant_id})
        return create_response(200, "Cotización eliminada")
    except Exception as e:
        return handle_exception(e, event)


# ============================================================
# CONVERTIR A ORDEN DE SERVICIO
# ============================================================
@logger.inject_lambda_context
def convertir_a_os_handler(event, context):
    """Crea una nueva OS copiando cliente, vehículo y puntos de la cotización.

    Marca la cotización como CONVERTIDA y guarda os_destino_id para trazabilidad.
    No registra inventario ni cobros — eso ocurre en el flujo normal de OS.
    """
    try:
        claims = get_claims(event)
        tenant_id = claims.get("custom:tenant_id")
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        cot_id = (event.get("pathParameters") or {}).get("id")
        oid, err = parse_object_id(cot_id)
        if err:
            return create_response(400, err)

        db = get_tenant_db(tenant_id)
        cot = db["cotizaciones"].find_one({"_id": oid, "tenant_id": tenant_id})
        if not cot:
            return create_response(404, "Cotización no encontrada")
        if cot.get("status") == "CONVERTIDA" and cot.get("os_destino_id"):
            return create_response(409, "La cotización ya fue convertida.", {
                "os_id": cot.get("os_destino_id"),
            })

        sucursal_id = cot.get("sucursal_id")
        if not sucursal_id:
            return create_response(400, "La cotización no tiene sucursal_id; no se puede convertir.")

        # Folio OS server-side
        from src.handlers.admin.folios_manager import _get_next_folio_internal
        folio_os = _get_next_folio_internal(tenant_id, "os", sucursal_id)

        puntos = cot.get("puntosArreglar") or []
        totales = _calcular_totales_orden(puntos)
        now = datetime.utcnow()

        os_doc = {
            "tenant_id": tenant_id,
            "sucursal_id": sucursal_id,
            "folio": folio_os,
            "estado": "RECEPCION",
            "cliente_snapshot": cot.get("cliente_snapshot") or {},
            "vehiculo_snapshot": cot.get("vehiculo_snapshot") or {},
            "puntosArreglar": puntos,
            "subtotal": totales["subtotal"],
            "iva": totales["iva"],
            "total": totales["total"],
            "kilometraje": cot.get("kilometraje"),
            "cotizacion_origen_id": str(cot["_id"]),
            "createdAt": now,
            "updatedAt": now,
            "pagada": False,
        }
        os_res = db["ordenes_servicio"].insert_one(os_doc)

        db["cotizaciones"].update_one(
            {"_id": oid},
            {"$set": {
                "status": "CONVERTIDA",
                "os_destino_id": str(os_res.inserted_id),
                "os_destino_folio": folio_os,
                "updatedAt": now,
            }},
        )

        return create_response(201, "Cotización convertida a Orden de Servicio", {
            "os_id": str(os_res.inserted_id),
            "os_folio": folio_os,
            "cotizacion_id": str(cot["_id"]),
        })
    except Exception as e:
        return handle_exception(e, event)
