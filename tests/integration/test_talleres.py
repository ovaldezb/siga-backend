import json
from src.handlers.admin.talleres_manager import create_taller_handler, list_talleres_handler, update_taller_handler

from unittest.mock import patch

# Keep existing tests...
def test_create_taller_success(mock_db):
    """Verifica la creación de un taller con sus datos básicos, incluyendo adminTelefono."""
    event = {
        "body": json.dumps({
            "nombreComercial": "Auto Service Pro",
            "modeloLicencia": "PREMIUM",
            "adminEmail": "admin@test.com",
            "adminNombre": "Juan",
            "adminApellido": "Perez",
            "adminTelefono": "5512345678"
        })
    }
    
    # Mockeamos el cliente de Cognito para que no intente conectar a AWS
    with patch('src.handlers.admin.talleres_manager.client') as mock_cognito:
        response = create_taller_handler(event, None)
        assert response['statusCode'] == 201
    
    data = json.loads(response['body'])['data']
    assert data['nombreComercial'] == "Auto Service Pro"
    assert data['adminEmail'] == "admin@test.com"
    assert data['adminTelefono'] == "5512345678"
    assert "tenantId" in data
    assert "proximaFechaCorte" in data
    assert "proximaFechaPago" in data

    # Verificar que adminTelefono esté persistido en la DB de plataforma
    db_platform = mock_db["_platform"]
    taller_db = db_platform.talleres.find_one({"tenantId": data["tenantId"]})
    assert taller_db is not None
    assert taller_db["adminTelefono"] == "5512345678"

def test_create_taller_prepago_configs(mock_db):
    """Verifica que el cálculo de fechas de pre-pago responda a mesesCargo y diasPrueba."""
    # Caso 1: 15 días de prueba (con días de prueba, corte es al fin de la prueba, pago +10 días)
    event_con_prueba = {
        "body": json.dumps({
            "nombreComercial": "Auto Service Pro Trial",
            "adminEmail": "admin_trial@test.com",
            "adminNombre": "Juan",
            "adminApellido": "Perez",
            "diasPrueba": 15,
            "mesesCargo": 1,
            "fechaAlta": "2026-01-01T00:00:00Z"
        })
    }
    
    with patch('src.handlers.admin.talleres_manager.client') as mock_cognito:
        response = create_taller_handler(event_con_prueba, None)
        assert response['statusCode'] == 201
    
    data = json.loads(response['body'])['data']
    # 2026-01-01 + 15 días de prueba = 2026-01-16 (fin del periodo de prueba = fecha de corte)
    # proximaFechaPago = 2026-01-16 + 10 días = 2026-01-26
    assert data['proximaFechaCorte'].startswith("2026-01-16")
    assert data['proximaFechaPago'].startswith("2026-01-26")

    # Caso 2: 0 días de prueba, 1 mes de cargo
    event_sin_prueba = {
        "body": json.dumps({
            "nombreComercial": "Auto Service Pro Normal",
            "adminEmail": "admin_normal@test.com",
            "adminNombre": "Carlos",
            "adminApellido": "Gomez",
            "diasPrueba": 0,
            "mesesCargo": 1,
            "fechaAlta": "2026-01-01T00:00:00Z"
        })
    }

    with patch('src.handlers.admin.talleres_manager.client') as mock_cognito:
        response2 = create_taller_handler(event_sin_prueba, None)
        assert response2['statusCode'] == 201

    data2 = json.loads(response2['body'])['data']
    # 2026-01-01 + 1 mes de cargo = 2026-02-01
    # proximaFechaPago = 2026-01-01 + 10 días = 2026-01-11
    assert data2['proximaFechaCorte'].startswith("2026-02-01")
    assert data2['proximaFechaPago'].startswith("2026-01-11")

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

def test_update_taller_dates(mock_db):
    """Verifica que un SUPER_ADMIN pueda actualizar proximaFechaCorte y proximaFechaPago."""
    db_platform = mock_db["_platform"]
    from bson import ObjectId
    from datetime import datetime
    taller_id = db_platform.talleres.insert_one({
        "nombreComercial": "Taller Test",
        "tenantId": "T_TEST",
        "proximaFechaCorte": datetime(2026, 1, 1),
        "proximaFechaPago": datetime(2026, 1, 11),
        "vendedor": "Pedro",
        "usuarios": 5,
        "sucursales": 2
    }).inserted_id

    event = {
        "pathParameters": {"id": str(taller_id)},
        "body": json.dumps({
            "proximaFechaCorte": "2026-05-15T00:00:00Z",
            "proximaFechaPago": "2026-05-25T00:00:00Z",
            "vendedor": "Pedro Picapiedra",
            "usuarios": 10,
            "sucursales": 5
        }),
        "requestContext": {
            "authorizer": {
                "claims": {
                    "cognito:groups": "SUPER_ADMIN"
                }
            }
        }
    }

    response = update_taller_handler(event, None)
    assert response['statusCode'] == 200
    data = json.loads(response['body'])['data']
    assert data['proximaFechaCorte'].startswith("2026-05-15")
    assert data['proximaFechaPago'].startswith("2026-05-25")
    assert data['vendedor'] == "Pedro Picapiedra"
    assert data['usuarios'] == 10
    assert data['sucursales'] == 5

    # Verificar en la DB
    updated = db_platform.talleres.find_one({"_id": ObjectId(taller_id)})
    assert updated["proximaFechaCorte"] == datetime(2026, 5, 15)
    assert updated["proximaFechaPago"] == datetime(2026, 5, 25)
    assert updated["vendedor"] == "Pedro Picapiedra"
    assert updated["usuarios"] == 10
    assert updated["sucursales"] == 5
