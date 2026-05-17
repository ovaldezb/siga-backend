"""Append-only audit log para ordenes_servicio (item #16 — auditoría inmutable).

`bitacora_estados` vive dentro del doc de la OS y se puede perder si alguien
edita el documento o se hace un $set descuidado. Esta collection paralela
`os_events` es append-only, guarda actor + ip cuando están disponibles y nunca
se modifica (los handlers solo hacen insert_one).

Dual-write: hasta que el frontend migre a leer eventos desde aquí, los handlers
siguen escribiendo `bitacora_estados` en el doc de la OS. Las dos vistas deben
converger; si divergen, esta es la fuente de verdad.

Tipos de evento:
- `os.created`        — nueva OS, payload incluye folio + estado inicial.
- `os.estado_changed` — cambio de estado, payload {from, to, motivo?}.
- `os.deleted`        — borrado de la OS.
- `os.payment`        — registro de pago / cobro (opcional para fase 2).
"""
from datetime import datetime
from typing import Optional
from aws_lambda_powertools import Logger
from src.shared.utils.date_utils import iso_utc

logger = Logger()

# Por tenant, recordamos si ya garantizamos el índice en este container Lambda
# (create_index es idempotente pero llamarlo en cada invocación es overhead).
_ensured_indexes_tenants: set[str] = set()


def _ensure_os_events_indexes(db, tenant_id: str) -> None:
    if tenant_id in _ensured_indexes_tenants:
        return
    try:
        # Lookups por OS (incluyendo línea de tiempo ordenada por ts).
        db.os_events.create_index([("orden_id", 1), ("ts", 1)], name="orden_ts")
        # Auditorías globales por actor (poder responder "¿qué cambió Juan ayer?").
        db.os_events.create_index([("actor", 1), ("ts", -1)], name="actor_ts")
        _ensured_indexes_tenants.add(tenant_id)
    except Exception as idx_err:
        logger.warning(f"No se pudo verificar índice de os_events: {idx_err}")


def _actor_from_claims(claims: Optional[dict]) -> str:
    if not claims:
        return "system"
    return claims.get("email") or claims.get("name") or claims.get("sub") or "system"


def _request_meta(event: Optional[dict]) -> dict:
    """Extrae ip y user_agent si vienen en el event de API Gateway."""
    if not event:
        return {}
    ctx = event.get("requestContext") or {}
    identity = ctx.get("identity") or {}
    headers = event.get("headers") or {}
    meta = {}
    ip = identity.get("sourceIp")
    if ip:
        meta["ip"] = ip
    ua = headers.get("User-Agent") or headers.get("user-agent")
    if ua:
        meta["user_agent"] = ua[:200]  # cortar largos, no necesitamos la URL entera
    return meta


def append_os_event(
    db,
    tenant_id: str,
    orden_id: str,
    tipo: str,
    payload: Optional[dict] = None,
    claims: Optional[dict] = None,
    event: Optional[dict] = None,
) -> None:
    """Inserta un evento append-only. Nunca lanza excepciones al caller — el log
    es secundario; si falla, dejamos warning pero la operación principal sigue.

    Args:
        db: handle de la BD del tenant.
        tenant_id: para auditar índices por container.
        orden_id: id de la OS (string del ObjectId).
        tipo: ver constantes en `OS_EVENT_*`.
        payload: dict serializable. Convención: usar `from`/`to` para cambios.
        claims: claims de Cognito (de event.requestContext.authorizer.claims).
        event: event completo de Lambda — extrae ip y user-agent.
    """
    try:
        _ensure_os_events_indexes(db, tenant_id)
        doc = {
            "orden_id": str(orden_id),
            "tipo": tipo,
            "ts": datetime.utcnow(),
            "actor": _actor_from_claims(claims),
            "payload": payload or {},
        }
        meta = _request_meta(event)
        if meta:
            doc["meta"] = meta
        db.os_events.insert_one(doc)
    except Exception as e:
        logger.warning(f"No se pudo registrar os_event {tipo} para {orden_id}: {e}")


def list_os_events(db, orden_id: str, limit: int = 200) -> list[dict]:
    """Devuelve eventos en orden cronológico (más viejo primero) — apto para timeline."""
    cursor = db.os_events.find({"orden_id": str(orden_id)}).sort("ts", 1).limit(limit)
    out = []
    for ev in cursor:
        ev["id"] = str(ev.pop("_id"))
        if isinstance(ev.get("ts"), datetime):
            ev["ts"] = iso_utc(ev["ts"])
        out.append(ev)
    return out


# Constantes para evitar typos en los handlers.
OS_EVENT_CREATED = "os.created"
OS_EVENT_ESTADO_CHANGED = "os.estado_changed"
OS_EVENT_DELETED = "os.deleted"
OS_EVENT_PAYMENT = "os.payment"
