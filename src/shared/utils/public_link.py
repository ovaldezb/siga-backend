"""Firma de tokens para enlaces públicos (sin Cognito).

Extraído de `handlers/ordenes/cliente_link_manager.py` cuando apareció el segundo
consumidor (portal de flotillas): la implementación HMAC y el secreto deben ser
uno solo para los dos flujos.

Formato del token: `b64u(payload_json).b64u(hmac_sha256(payload_json))`.
El payload es JSON con claves sort_keys para que la firma sea reproducible.
No usamos PyJWT a propósito — solo stdlib, para no cargar dependencias al layer.
"""

import base64
import hashlib
import hmac
import json
import os
import re
from datetime import datetime
from typing import Optional

from aws_lambda_powertools import Logger

logger = Logger()


def get_secret() -> bytes:
    s = os.environ.get('CLIENT_LINK_SECRET', '')
    if not s:
        # Fallback dev-only para no romper local sin SSM. Logueamos warning para detectarlo.
        s = 'dev-fallback-' + os.environ.get('MONGO_HOST', 'localhost')
        logger.warning('CLIENT_LINK_SECRET no configurado; usando fallback de desarrollo')
    return s.encode('utf-8')


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b'=').decode('ascii')


def b64u_dec(s: str) -> bytes:
    pad = '=' * (-len(s) % 4)
    return base64.urlsafe_b64decode((s + pad).encode('ascii'))


def sign(payload: dict) -> str:
    body = json.dumps(payload, separators=(',', ':'), sort_keys=True).encode('utf-8')
    sig = hmac.new(get_secret(), body, hashlib.sha256).digest()
    return f"{b64u(body)}.{b64u(sig)}"


def verify(token: str) -> Optional[dict]:
    """Devuelve el payload si la firma es válida y no expiró; None en cualquier otro caso."""
    try:
        body_b64, sig_b64 = token.split('.', 1)
        body = b64u_dec(body_b64)
        sig = b64u_dec(sig_b64)
        expected = hmac.new(get_secret(), body, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(body.decode('utf-8'))
        exp = payload.get('exp')
        if exp and datetime.utcnow().timestamp() > exp:
            return None
        return payload
    except Exception:
        return None


def hash_answer(answer: str) -> str:
    """HMAC del secreto de acceso (respuesta de challenge o PIN). Hex, comparable con compare_digest."""
    return hmac.new(get_secret(), answer.encode('utf-8'), hashlib.sha256).hexdigest()


def digits(s: str) -> str:
    return re.sub(r'\D', '', s or '')


def client_ip(event) -> str:
    rc = event.get('requestContext') or {}
    # httpApi (v2) trae http.sourceIp; v1 lo deja en identity.sourceIp.
    v2 = ((rc.get('http') or {}).get('sourceIp')) or ''
    if v2:
        return v2
    return (rc.get('identity') or {}).get('sourceIp', '')


def user_agent(event) -> str:
    h = event.get('headers') or {}
    return h.get('User-Agent') or h.get('user-agent') or ''
