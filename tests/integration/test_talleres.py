import json
from src.handlers.admin.talleres_manager import create_taller_handler, list_talleres_handler

from unittest.mock import patch

def test_create_taller_success(mock_db):
    """Verifica la creación de un taller con sus datos básicos."""
    event = {
        "body": json.dumps({
            "nombreComercial": "Auto Service Pro",
            "modeloLicencia": "PREMIUM",
            "adminEmail": "admin@test.com",
            "adminNombre": "Juan",
            "adminApellido": "Perez"
        })
    }
    
    # Mockeamos el cliente de Cognito para que no intente conectar a AWS
    with patch('src.handlers.admin.talleres_manager.client') as mock_cognito:
        response = create_taller_handler(event, None)
        assert response['statusCode'] == 201
    
    data = json.loads(response['body'])['data']
    assert data['nombreComercial'] == "Auto Service Pro"
    assert data['adminEmail'] == "admin@test.com"
    assert "tenantId" in data

def test_list_talleres_isolation(mock_db):
    """Verifica que el listado de talleres devuelva los registros de la DB de plataforma."""
    db_platform = mock_db["_platform"]
    db_platform.talleres.insert_many([
        {"nombreComercial": "Taller A", "tenantId": "T1"},
        {"nombreComercial": "Taller B", "tenantId": "T2"}
    ])
    
    event = {} 
    response = list_talleres_handler(event, None)
    
    assert response['statusCode'] == 200
    data = json.loads(response['body'])['data']
    assert len(data) >= 2
