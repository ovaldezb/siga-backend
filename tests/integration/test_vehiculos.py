import json
from unittest.mock import patch, MagicMock
from src.handlers.vehiculos.vehiculos_manager import decode_vin_handler

def test_decode_vin_success(mock_db):
    """Verifica que se decodifique correctamente un VIN válido mockeando la respuesta de la NHTSA."""
    tenant_id = "test-tenant-123"
    
    mock_response = MagicMock()
    mock_response.__enter__.return_value = mock_response
    mock_response.read.return_value = json.dumps({
        "Count": 1,
        "Message": "Results returned successfully",
        "SearchCriteria": "VIN: 1FA6P8CF0H5XXXXXX",
        "Results": [
            {
                "SuggestedVIN": "1FA6P8CF0H5XXXXXX",
                "Make": "FORD",
                "Model": "MUSTANG",
                "ModelYear": "2017",
                "EngineHP": "310"
            }
        ]
    }).encode('utf-8')

    event = {
        "pathParameters": {
            "vin": "1FA6P8CF0H5XXXXXX"
        },
        "requestContext": {
            "authorizer": {
                "claims": {
                    "custom:tenant_id": tenant_id
                }
            }
        }
    }

    # Patch urllib.request.urlopen directly
    with patch('urllib.request.urlopen', return_value=mock_response):
        response = decode_vin_handler(event, None)
        
    assert response['statusCode'] == 200
    body = json.loads(response['body'])
    assert body['message'] == "VIN decodificado exitosamente"
    assert body['data']['Results'][0]['Make'] == "FORD"
    assert body['data']['Results'][0]['Model'] == "MUSTANG"

def test_decode_vin_invalid_length(mock_db):
    """Verifica que retorne 400 si el VIN no tiene exactamente 17 caracteres."""
    tenant_id = "test-tenant-123"
    event = {
        "pathParameters": {
            "vin": "SHORT"
        },
        "requestContext": {
            "authorizer": {
                "claims": {
                    "custom:tenant_id": tenant_id
                }
            }
        }
    }

    response = decode_vin_handler(event, None)
    assert response['statusCode'] == 400
    body = json.loads(response['body'])
    assert "17 caracteres" in body['message']

def test_decode_vin_error_external_service(mock_db):
    """Verifica que retorne 502 si la API de la NHTSA falla o no se puede conectar."""
    tenant_id = "test-tenant-123"
    event = {
        "pathParameters": {
            "vin": "1FA6P8CF0H5XXXXXX"
        },
        "requestContext": {
            "authorizer": {
                "claims": {
                    "custom:tenant_id": tenant_id
                }
            }
        }
    }

    import urllib.error
    
    # Simular una excepción URLError al hacer urlopen
    with patch('urllib.request.urlopen', side_effect=urllib.error.URLError("Connection reset")):
        response = decode_vin_handler(event, None)
        
    assert response['statusCode'] == 502
    body = json.loads(response['body'])
    assert "Error al conectar" in body['message']
