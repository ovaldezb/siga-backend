import pytest
import mongomock
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

@pytest.fixture(autouse=True)
def mock_db():
    """
    Este fixture reemplaza la conexión a MongoDB por un cliente en memoria.
    """
    mock_client = mongomock.MongoClient()
    
    with patch('src.shared.infrastructure.database.MongoDBConnection.get_client', return_value=mock_client):
        with patch('src.shared.infrastructure.database.get_platform_db', return_value=mock_client["_platform"]):
            yield mock_client
