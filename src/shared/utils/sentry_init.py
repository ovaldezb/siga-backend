"""Inicialización idempotente de Sentry para las Lambdas de siga-backend.

Importar este módulo desde cualquier handler garantiza que Sentry esté
configurado antes de que se ejecute código de negocio. Si `SENTRY_DSN`
no está seteado, el SDK queda en no-op y no genera tráfico ni costo.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

_initialized = False


def _maybe_float(value: Optional[str], default: float) -> float:
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def init_sentry() -> None:
    global _initialized
    if _initialized:
        return
    _initialized = True

    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        return

    try:
        import sentry_sdk
        from sentry_sdk.integrations.aws_lambda import AwsLambdaIntegration
    except ImportError:
        return

    sentry_sdk.init(
        dsn=dsn,
        environment=os.environ.get("SENTRY_ENVIRONMENT") or os.environ.get("STAGE") or "dev",
        traces_sample_rate=_maybe_float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE"), 0.0),
        integrations=[AwsLambdaIntegration(timeout_warning=True)],
        send_default_pii=False,
    )


def capture_with_event(exc: BaseException, event: Optional[Dict[str, Any]] = None) -> None:
    """Captura una excepción enriquecida con tenant_id / email del evento Lambda."""
    if not _initialized:
        init_sentry()
    if not _initialized:
        return
    try:
        import sentry_sdk
    except ImportError:
        return

    with sentry_sdk.new_scope() as scope:
        claims = (
            (event or {})
            .get("requestContext", {})
            .get("authorizer", {})
            .get("claims", {})
            or {}
        )
        tenant_id = claims.get("custom:tenant_id")
        if tenant_id:
            scope.set_tag("tenant_id", tenant_id)
        email = claims.get("email")
        if email:
            scope.set_user({"email": email})
        path = (event or {}).get("path") or (event or {}).get("resource")
        if path:
            scope.set_tag("api.path", path)
        sentry_sdk.capture_exception(exc)


init_sentry()
