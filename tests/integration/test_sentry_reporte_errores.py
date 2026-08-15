"""Sentry reporta de verdad: prueba end-to-end contra un Sentry falso local.

El cableado de Sentry lleva meses escrito pero desconectado (sin DSN no manda
nada), así que nadie había comprobado que funcione. Estos tests levantan un
servidor HTTP que hace de Sentry, apuntan el DSN ahí y verifican que un 500 real
—pasando por `handle_exception`, el mismo camino que usan todos los handlers—
llega al otro lado con sus tags de tenant y usuario.

También fija la otra mitad del contrato: sin DSN no se manda absolutamente nada
(el SDK queda no-op, sin tráfico ni costo).
"""
import gzip
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from src.shared.utils import response_handler, sentry_init


class _SentryFalso(BaseHTTPRequestHandler):
    """Recibe los envelopes que manda el SDK y los guarda en el servidor."""

    def do_POST(self):
        largo = int(self.headers.get("Content-Length", 0))
        crudo = self.rfile.read(largo)
        if self.headers.get("Content-Encoding") == "gzip":
            crudo = gzip.GzipFile(fileobj=io.BytesIO(crudo)).read()
        self.server.envelopes.append(crudo.decode("utf-8", errors="replace"))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass  # sin ruido en la salida de pytest


@pytest.fixture
def sentry_falso():
    """Servidor local que hace de Sentry. Devuelve (dsn, envelopes_recibidos)."""
    servidor = HTTPServer(("127.0.0.1", 0), _SentryFalso)
    servidor.envelopes = []
    hilo = threading.Thread(target=servidor.serve_forever, daemon=True)
    hilo.start()
    puerto = servidor.server_address[1]
    yield f"http://llavepublica@127.0.0.1:{puerto}/1", servidor.envelopes
    servidor.shutdown()
    servidor.server_close()


@pytest.fixture(autouse=True)
def sentry_limpio(monkeypatch):
    """Cada test arranca con Sentry sin inicializar y sin DSN heredado."""
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    monkeypatch.delenv("SENTRY_ENVIRONMENT", raising=False)
    monkeypatch.delenv("SENTRY_TRACES_SAMPLE_RATE", raising=False)
    monkeypatch.setattr(sentry_init, "_initialized", False)
    yield
    import sentry_sdk
    sentry_sdk.get_global_scope().set_client(None)
    monkeypatch.setattr(sentry_init, "_initialized", False)


def _evento_lambda():
    """Evento de API Gateway como el que reciben los handlers en producción."""
    return {
        "path": "/ordenes/123",
        "requestContext": {"authorizer": {"claims": {
            "custom:tenant_id": "tenant-express",
            "email": "asesor@taller.com",
        }}},
    }


def _enviar_error(dsn, monkeypatch, evento=None):
    """Dispara un 500 por el mismo camino que cualquier handler y espera el flush.

    La excepción se lanza de verdad dentro de un try/except: así lleva traceback,
    igual que el `except Exception as e: return handle_exception(e, event)` que
    cierra todos los handlers del backend.
    """
    import sentry_sdk

    monkeypatch.setenv("SENTRY_DSN", dsn)
    monkeypatch.setenv("SENTRY_ENVIRONMENT", "production")
    sentry_init.init_sentry()

    try:
        raise RuntimeError("Explotó el cálculo de totales")
    except RuntimeError as e:
        respuesta = response_handler.handle_exception(e, evento)
    sentry_sdk.flush(timeout=5)
    return respuesta


def _eventos(envelopes):
    """Extrae los eventos de error de los envelopes (ignora headers y reportes)."""
    eventos = []
    for envelope in envelopes:
        for linea in envelope.splitlines():
            try:
                doc = json.loads(linea)
            except ValueError:
                continue
            if isinstance(doc, dict) and doc.get("exception"):
                eventos.append(doc)
    return eventos


def test_un_500_llega_a_sentry_con_tenant_y_usuario(sentry_falso, monkeypatch):
    dsn, envelopes = sentry_falso

    respuesta = _enviar_error(dsn, monkeypatch, _evento_lambda())

    # El handler sigue respondiendo lo de siempre: reportar no cambia la respuesta.
    assert respuesta["statusCode"] == 500
    assert "error interno" in json.loads(respuesta["body"])["message"]

    eventos = _eventos(envelopes)
    assert eventos, "Sentry no recibió nada: el reporte de errores NO está funcionando."
    assert len(eventos) == 1, "Un error debe generar UN evento, no duplicados que gastan cuota."

    evento = eventos[0]
    assert evento["exception"]["values"][0]["value"] == "Explotó el cálculo de totales"
    assert evento["environment"] == "production"
    # Lo que hace útil el reporte en multi-tenant: saber de qué taller vino.
    assert evento["tags"]["tenant_id"] == "tenant-express"
    assert evento["tags"]["api.path"] == "/ordenes/123"
    assert evento["user"]["email"] == "asesor@taller.com"


def test_sin_dsn_no_se_manda_nada(sentry_falso, monkeypatch):
    """El estado actual de producción: SDK apagado, cero tráfico."""
    _, envelopes = sentry_falso

    monkeypatch.setenv("SENTRY_DSN", "")
    sentry_init.init_sentry()
    try:
        raise RuntimeError("boom")
    except RuntimeError as e:
        respuesta = response_handler.handle_exception(e, _evento_lambda())

    assert respuesta["statusCode"] == 500
    assert envelopes == []


def test_los_errores_de_cliente_no_gastan_cuota(sentry_falso, monkeypatch):
    """Un 400 por ObjectId inválido o campo faltante NO es un incidente."""
    import sentry_sdk
    dsn, envelopes = sentry_falso

    monkeypatch.setenv("SENTRY_DSN", dsn)
    sentry_init.init_sentry()
    respuesta = response_handler.handle_exception(ValueError("placas inválidas"), _evento_lambda())
    sentry_sdk.flush(timeout=5)

    assert respuesta["statusCode"] == 400
    assert envelopes == []


def test_un_error_sin_evento_tambien_se_reporta(sentry_falso, monkeypatch):
    """Handlers que llaman handle_exception(e) sin pasar el event siguen reportando."""
    dsn, envelopes = sentry_falso

    _enviar_error(dsn, monkeypatch, evento=None)

    eventos = _eventos(envelopes)
    assert eventos, "Un 500 sin evento debería reportarse igual."
    assert eventos[0]["exception"]["values"][0]["value"] == "Explotó el cálculo de totales"
