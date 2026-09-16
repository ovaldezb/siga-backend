"""Flotillas (clientes corporativos) — item #7 del audit 2026-05-17.

Una flotilla agrupa N clientes que pertenecen al mismo corporativo. La vista
de flotilla resume vehículos totales, OS pendientes y monto en pipeline para
que el asesor tenga al cliente flotillero como una sola entidad operativa.

La asignación se hace por `flotilla_id` en el documento del cliente — se
mantiene la collection `clientes` como fuente única para no romper handlers
existentes (ventas, OS, etc.).
"""
import json
from bson import ObjectId
from aws_lambda_powertools import Logger

from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.utils.auth_utils import parse_object_id, get_claims
from src.shared.infrastructure.database import get_tenant_db
from src.shared.utils.indexes import ensure_indexes
from src.shared.utils.date_utils import iso_utc

logger = Logger()

ALLOWED_FIELDS = {
    "nombre", "razon_social", "rfc", "telefono", "email",
    "contacto_nombre", "contacto_puesto", "notas", "activo",
}

# Estados de OS considerados "abiertos" para el agregado del resumen.
ESTADOS_PENDIENTES = ["RECEPCION", "COTIZADO", "APROBADO", "EN_PROCESO"]

# Tope de unidades que viajan en el detalle. El conteo (`num_vehiculos`) es exacto
# aunque la lista se recorte; el flag `vehiculos_truncados` avisa al UI.
MAX_VEHICULOS_DETALLE = 300


def _serialize(doc: dict) -> dict:
    doc["id"] = str(doc.pop("_id"))
    return doc


def list_flotillas_handler(event, context):
    try:
        claims =get_claims(event)
        tenant_id = claims.get("custom:tenant_id")
        if not tenant_id:
            return create_response(403, "No tenantId")

        db = get_tenant_db(tenant_id)
        ensure_indexes(db, tenant_id)

        flotillas = [_serialize(d) for d in db.flotillas.find()]

        # Enriquecer con conteos agregados (una sola pasada).
        if flotillas:
            ids = [f["id"] for f in flotillas]
            miembros = list(db.clientes.find(
                {"flotilla_id": {"$in": ids}}, {"flotilla_id": 1}
            ))
            cmap: dict[str, list[str]] = {}
            for m in miembros:
                cmap.setdefault(m["flotilla_id"], []).append(str(m["_id"]))

            # Unidades por flotilla: una sola agregación para todas, agrupando por
            # cliente. La tarjeta del listado muestra el parque vehicular sin tener
            # que abrir el detalle — era la pregunta más repetida del asesor.
            todos_los_clientes = [cid for lista in cmap.values() for cid in lista]
            vmap = {}
            if todos_los_clientes:
                vmap = {
                    v["_id"]: v["count"]
                    for v in db.vehiculos.aggregate([
                        {"$match": {"cliente_id": {"$in": todos_los_clientes}}},
                        {"$group": {"_id": "$cliente_id", "count": {"$sum": 1}}},
                    ])
                }

            for f in flotillas:
                miembros_f = cmap.get(f["id"], [])
                f["num_clientes"] = len(miembros_f)
                f["num_vehiculos"] = sum(vmap.get(cid, 0) for cid in miembros_f)

        return create_response(200, "Flotillas obtenidas", flotillas)
    except Exception as e:
        return handle_exception(e)


def get_flotilla_handler(event, context):
    """Devuelve la flotilla + resumen agregado (clientes, vehículos, OS, pipeline)."""
    try:
        claims =get_claims(event)
        tenant_id = claims.get("custom:tenant_id")
        if not tenant_id:
            return create_response(403, "No tenantId")

        flot_id = event["pathParameters"]["id"]
        oid, err = parse_object_id(flot_id)
        if err:
            return create_response(400, err)

        db = get_tenant_db(tenant_id)
        flotilla = db.flotillas.find_one({"_id": oid})
        if not flotilla:
            return create_response(404, "Flotilla no encontrada")
        flotilla = _serialize(flotilla)

        # Clientes miembros (proyección ligera para la lista).
        clientes = list(db.clientes.find(
            {"flotilla_id": flot_id},
            {"nombre": 1, "apellido_paterno": 1, "apellido_materno": 1,
             "telefono": 1, "email": 1, "rfc": 1,
             "limite_credito": 1, "dias_credito": 1}
        ))
        for c in clientes:
            c["id"] = str(c.pop("_id"))
            c["limite_credito"] = float(c.get("limite_credito") or 0)
            c["dias_credito"] = int(c.get("dias_credito") or 0)
            c["saldo_credito"] = 0.0
            c["num_vehiculos"] = 0
        cliente_ids = [c["id"] for c in clientes]
        cmap = {c["id"]: c for c in clientes}

        # Vehículos de la flotilla. Se devuelve la lista, no sólo el conteo: la
        # ficha necesita enseñar QUÉ unidades la componen — un vehículo pertenece a
        # la flotilla a través de su dueño, y sin verlas nadie entendía la liga.
        num_vehiculos = 0
        vehiculos = []
        if cliente_ids:
            # El conteo va por agregación aparte para que no dependa del tope de la
            # lista: una flotilla con más unidades que MAX_VEHICULOS_DETALLE sigue
            # reportando su parque vehicular completo.
            for row in db.vehiculos.aggregate([
                {"$match": {"cliente_id": {"$in": cliente_ids}}},
                {"$group": {"_id": "$cliente_id", "count": {"$sum": 1}}},
            ]):
                num_vehiculos += int(row["count"])
                if row["_id"] in cmap:
                    cmap[row["_id"]]["num_vehiculos"] = int(row["count"])

            for v in db.vehiculos.find(
                {"cliente_id": {"$in": cliente_ids}},
                {"marca": 1, "modelo": 1, "anio": 1, "placas": 1, "color": 1,
                 "kilometraje": 1, "cliente_id": 1},
            ).limit(MAX_VEHICULOS_DETALLE):
                v["id"] = str(v.pop("_id"))
                duenio = cmap.get(v.get("cliente_id"), {})
                v["cliente_nombre"] = " ".join(filter(None, [
                    duenio.get("nombre"), duenio.get("apellido_paterno"),
                ])).strip()
                vehiculos.append(v)

        # OS pendientes y monto en pipeline.
        os_pend = 0
        monto_pipeline = 0.0
        if cliente_ids:
            agg = list(db.ordenes_servicio.aggregate([
                {"$match": {
                    "cliente_snapshot.id": {"$in": cliente_ids},
                    "estado": {"$in": ESTADOS_PENDIENTES},
                }},
                {"$group": {
                    "_id": None,
                    "count": {"$sum": 1},
                    "monto": {"$sum": {"$ifNull": ["$total", 0]}},
                }},
            ]))
            if agg:
                os_pend = int(agg[0]["count"])
                monto_pipeline = float(agg[0]["monto"])

        # Para la facturación consolidada exponemos los IDs de OS finalizadas no
        # facturadas (el botón del UI usa esto para mostrar el bloque "listo para
        # facturar" — el flujo real CFDI vive en el módulo facturación, item #13).
        os_finalizadas = 0
        monto_facturable = 0.0
        if cliente_ids:
            agg2 = list(db.ordenes_servicio.aggregate([
                {"$match": {
                    "cliente_snapshot.id": {"$in": cliente_ids},
                    "estado": {"$in": ["FINALIZADO", "ENTREGADO"]},
                    "facturada": {"$ne": True},
                }},
                {"$group": {
                    "_id": None,
                    "count": {"$sum": 1},
                    "monto": {"$sum": {"$ifNull": ["$total", 0]}},
                }},
            ]))
            if agg2:
                os_finalizadas = int(agg2[0]["count"])
                monto_facturable = float(agg2[0]["monto"])

        # Crédito REAL de la flotilla. No hay línea de crédito a nivel flotilla: la
        # autoriza `ventas_manager` cliente por cliente contra `limite_credito`, y lo
        # consumido es la CxC viva (`ventas.saldo_pendiente > 0`) — mismo criterio
        # que usan clientes_manager y el POS. La flotilla sólo agrega a sus miembros;
        # antes aquí había un límite fijo de demostración que no era de nadie.
        credito_usado = 0.0
        if cliente_ids:
            for row in db.ventas.aggregate([
                {"$match": {
                    "cliente_id": {"$in": cliente_ids},
                    "saldo_pendiente": {"$gt": 0},
                }},
                {"$group": {"_id": "$cliente_id", "saldo": {"$sum": "$saldo_pendiente"}}},
            ]):
                saldo = round(float(row["saldo"]), 2)
                credito_usado += saldo
                if row["_id"] in cmap:
                    cmap[row["_id"]]["saldo_credito"] = saldo

        credito_limite = round(sum(c["limite_credito"] for c in clientes), 2)
        credito_usado = round(credito_usado, 2)
        flotilla["credito_limite"] = credito_limite
        flotilla["credito_usado"] = credito_usado
        flotilla["credito_disponible"] = round(credito_limite - credito_usado, 2)
        flotilla["clientes_con_credito"] = sum(1 for c in clientes if c["limite_credito"] > 0)

        flotilla["vehiculos"] = vehiculos
        flotilla["vehiculos_truncados"] = num_vehiculos > len(vehiculos)
        flotilla["clientes"] = clientes
        flotilla["num_clientes"] = len(clientes)
        flotilla["num_vehiculos"] = num_vehiculos
        flotilla["os_pendientes"] = os_pend
        flotilla["monto_pipeline"] = monto_pipeline
        flotilla["os_finalizadas_no_facturadas"] = os_finalizadas
        flotilla["monto_facturable"] = monto_facturable

        return create_response(200, "Detalle de flotilla", flotilla)
    except Exception as e:
        return handle_exception(e)


def create_flotilla_handler(event, context):
    try:
        claims =get_claims(event)
        tenant_id = claims.get("custom:tenant_id")
        if not tenant_id:
            return create_response(403, "No tenantId")

        body = json.loads(event.get("body", "{}"))
        if not body.get("nombre"):
            return create_response(400, "El campo 'nombre' es obligatorio.")

        doc = {k: body[k] for k in ALLOWED_FIELDS if k in body}
        doc.setdefault("activo", True)
        doc["tenant_id"] = tenant_id
        doc["createdAt"] = iso_utc()

        db = get_tenant_db(tenant_id)
        result = db.flotillas.insert_one(doc)
        doc["id"] = str(result.inserted_id)
        doc.pop("_id", None)
        return create_response(201, "Flotilla creada", doc)
    except Exception as e:
        return handle_exception(e)


def update_flotilla_handler(event, context):
    try:
        claims =get_claims(event)
        tenant_id = claims.get("custom:tenant_id")
        if not tenant_id:
            return create_response(403, "No tenantId")

        flot_id = event["pathParameters"]["id"]
        oid, err = parse_object_id(flot_id)
        if err:
            return create_response(400, err)

        body = json.loads(event.get("body", "{}"))
        update_doc = {k: body[k] for k in ALLOWED_FIELDS if k in body}
        if not update_doc:
            return create_response(400, "Nada que actualizar.")
        update_doc["updatedAt"] = iso_utc()

        db = get_tenant_db(tenant_id)
        from pymongo import ReturnDocument
        result = db.flotillas.find_one_and_update(
            {"_id": oid},
            {"$set": update_doc},
            return_document=ReturnDocument.AFTER,
        )
        if not result:
            return create_response(404, "Flotilla no encontrada")
        return create_response(200, "Flotilla actualizada", _serialize(result))
    except Exception as e:
        return handle_exception(e)


def delete_flotilla_handler(event, context):
    """Borra la flotilla. Bloquea si tiene clientes asignados — el usuario debe
    desasignarlos primero para evitar referencias colgadas."""
    try:
        claims =get_claims(event)
        tenant_id = claims.get("custom:tenant_id")
        if not tenant_id:
            return create_response(403, "No tenantId")

        flot_id = event["pathParameters"]["id"]
        oid, err = parse_object_id(flot_id)
        if err:
            return create_response(400, err)

        db = get_tenant_db(tenant_id)
        cnt = db.clientes.count_documents({"flotilla_id": flot_id})
        if cnt > 0:
            return create_response(
                409,
                f"No se puede eliminar: la flotilla tiene {cnt} cliente(s) asignado(s).",
            )

        result = db.flotillas.delete_one({"_id": oid})
        if result.deleted_count == 0:
            return create_response(404, "Flotilla no encontrada")
        return create_response(200, "Flotilla eliminada")
    except Exception as e:
        return handle_exception(e)
