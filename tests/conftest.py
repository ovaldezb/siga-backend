import pytest
import mongomock
from unittest.mock import patch, MagicMock

# Desactivar decoradores de Logger de PowerTools durante los tests
from aws_lambda_powertools import Logger
def mock_inject_lambda_context(*args, **kwargs):
    if len(args) > 0 and callable(args[0]): return args[0]
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
