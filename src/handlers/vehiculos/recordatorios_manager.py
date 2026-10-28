"""Seguimiento de los recordatorios de servicio (cambio de aceite, bujías, etc.).

La lista de vehículos a los que les toca servicio la arma la lambda Go
`recordatorios_list` (GET /recordatorios-servicio). Aquí sólo se registra qué hizo el
taller con cada uno, en `vehiculos.recordatorio_servicio`:

  · CONTACTADO  — ya se le llamó/escribió; vuelve a aparecer en `dias` (15 por default)
                  si para entonces no ha regresado al taller.
  · POSPUESTO   — el cliente pidió que se le busque más adelante (30 días por default).
  · DESCARTADO  — vendió el auto, ya no es cliente, etc.: no vuelve a aparecer.
  · REACTIVAR   — quita el seguimiento y el vehículo regresa a la lista.

Cada acción queda además en `recordatorios_historial` (las últimas 50).
"""
import json
from datetime import datetime, timedelta
from aws_lambda_powertools import Logger
from bson import ObjectId
from bson.errors import InvalidId

from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.infrastructure.database import get_tenant_db
from src.shared.utils.auth_utils import get_claims, es_mecanico

logger = Logger()

DIAS_POR_DEFAULT = {"CONTACTADO": 15, "POSPUESTO": 30}
ACCIONES = ("CONTACTADO", "POSPUESTO", "DESCARTADO", "REACTIVAR")
HISTORIAL_MAX = 50


@logger.inject_lambda_context
def registrar_seguimiento_handler(event, context):
    """POST /recordatorios-servicio/{id} — body {accion, dias?, nota?}; id = vehículo."""
    try:
        claims = get_claims(event)
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No autorizado")
        if es_mecanico(claims):
            return create_response(403, "Tu usuario no da seguimiento a clientes.")

        try:
            vehiculo_oid = ObjectId((event.get('pathParameters') or {}).get('id'))
        except (InvalidId, TypeError):
            return create_response(400, "ID de vehículo inválido.")

        body = json.loads(event.get('body') or '{}')
        accion = str(body.get('accion') or '').strip().upper()
        if accion not in ACCIONES:
            return create_response(400, "Acción inválida (CONTACTADO, POSPUESTO, DESCARTADO o REACTIVAR).")
        nota = str(body.get('nota') or '').strip()[:500]

        ahora = datetime.utcnow()
        usuario = claims.get('name') or claims.get('email') or claims.get('sub') or 'desconocido'
        hasta = None
        if accion in DIAS_POR_DEFAULT:
            try:
                dias = body.get('dias')
                dias = DIAS_POR_DEFAULT[accion] if dias in (None, '') else int(dias)
            except (TypeError, ValueError):
                return create_response(400, "Días inválidos.")
            if not 1 <= dias <= 365:
                return create_response(400, "Los días deben estar entre 1 y 365.")
            hasta = ahora + timedelta(days=dias)

        db = get_tenant_db(tenant_id)
        entrada = {"accion": accion, "nota": nota, "hasta": hasta, "usuario": usuario, "fecha": ahora}
        update = {
            "$push": {"recordatorios_historial": {"$each": [entrada], "$slice": -HISTORIAL_MAX}},
            "$set": {"updatedAt": ahora},
        }
        if accion == "REACTIVAR":
            update["$unset"] = {"recordatorio_servicio": ""}
        else:
            update["$set"]["recordatorio_servicio"] = {
                "estado": accion, "hasta": hasta, "nota": nota, "usuario": usuario, "fecha": ahora,
            }

        res = db["vehiculos"].update_one({"_id": vehiculo_oid}, update)
        if res.matched_count == 0:
            return create_response(404, "Vehículo no encontrado.")

        return create_response(200, "Seguimiento registrado", {
            "id": str(vehiculo_oid),
            "accion": accion,
            "hasta": hasta.isoformat() + "Z" if hasta else None,
        })
    except Exception as e:
        return handle_exception(e, event)
