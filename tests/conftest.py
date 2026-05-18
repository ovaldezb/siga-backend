import pytest
import mongomock
import mongomock.not_implemented
from unittest.mock import patch, MagicMock

# Desactivar decoradores de Logger de PowerTools durante los tests.
# Soporta los 3 estilos de uso del decorador:
#   1. @logger.inject_lambda_context              → bound method: args=(logger, func)
#   2. @Logger.inject_lambda_context              → unbound:      args=(func,)
#   3. @logger.inject_lambda_context(log_event=1) → con kwargs:   devuelve decorator
from aws_lambda_powertools import Logger
def mock_inject_lambda_context(*args, **kwargs):
    # Caso 1: bound method, args=(self, func) — devolver func tal cual.
    if len(args) >= 2 and callable(args[1]):
        return args[1]
    # Caso 2: invocado como función con la handler como único arg.
    if len(args) == 1 and callable(args[0]) and not isinstance(args[0], Logger):
        return args[0]
    # Caso 3: invocado con kwargs (o sin args) — devolver decorator no-op.
    return lambda f: f
Logger.inject_lambda_context = mock_inject_lambda_context


# --- Soporte de sesiones/transacciones para mongomock ------------------------
# mongomock no implementa start_session ni el kwarg session=. Los handlers de
# producción usan ambos para garantizar atomicidad en ventas/abonos. Para que
# los tests puedan ejercitar esos handlers, parchamos:
#  1) raise_for_feature → no-op, mongomock ignora silenciosamente session=.
#  2) MongoClient.start_session → context manager fake; el "session" devuelto
#     soporta start_transaction() pero las operaciones NO son atómicas (mongomock
#     no soporta rollback). Por eso los tests verifican happy path y errores
#     declarados por el handler, no estados intermedios de transacciones fallidas.
mongomock.not_implemented.ignore_feature('session')


class _FakeTx:
    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc_val, exc_tb):
        return False  # propagar excepciones


class _FakeSession:
    def start_transaction(self):
        return _FakeTx()
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


mongomock.MongoClient.start_session = lambda self, *a, **kw: _FakeSession()


@pytest.fixture(autouse=True)
def mock_db():
    """
    Este fixture reemplaza la conexión a MongoDB por un cliente en memoria.
    """
    mock_client = mongomock.MongoClient()

    with patch('src.shared.infrastructure.database.MongoDBConnection.get_client', return_value=mock_client):
        with patch('src.shared.infrastructure.database.get_platform_db', return_value=mock_client["_platform"]):
            yield mock_client
