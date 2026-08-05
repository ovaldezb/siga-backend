import json
from src.handlers.catalogos.sat_manager import search_sat_catalogos_handler

def test_search_sat_clavesat_by_description(mock_db):
    # Seed the mock database for catprodserv
    db = mock_db["_platform"]
    db.catprodserv.insert_many([
        {"clave": "10121600", "descripcion": "Alimento para pájaros y aves de corral"},
        {"clave": "25172502", "descripcion": "Aceite lubricante para motor de gasolina"},
        {"clave": "30101500", "descripcion": "Filtros de aire"}
    ])

    event = {
        "pathParameters": {"tipoBusqueda": "clavesat"},
        "queryStringParameters": {"q": "aceite"},
        "requestContext": {
            "authorizer": {
                "claims": {"custom:tenant_id": "taller_test"}
            }
        }
    }

    response = search_sat_catalogos_handler(event, None)
    assert response['statusCode'] == 200
    
    data = json.loads(response['body'])['data']
    assert len(data) == 1
    assert data[0]['clave'] == "25172502"
    assert "gasolina" in data[0]['descripcion']

def test_search_sat_unidad_by_description(mock_db):
    # Seed the mock database for unidad
    db = mock_db["_platform"]
    db.unidad.insert_many([
        {"clave": "19", "descripcion": "Camión cisterna"},
        {"clave": "H87", "descripcion": "Pieza"},
        {"clave": "KGM", "descripcion": "Kilogramo"}
    ])

    event = {
        "pathParameters": {"tipoBusqueda": "unidad"},
        "queryStringParameters": {"q": "pieza"},
        "requestContext": {
            "authorizer": {
                "claims": {"custom:tenant_id": "taller_test"}
            }
        }
    }

    response = search_sat_catalogos_handler(event, None)
    assert response['statusCode'] == 200
    
    data = json.loads(response['body'])['data']
    assert len(data) == 1
    assert data[0]['clave'] == "H87"
    assert data[0]['descripcion'] == "Pieza"

def test_search_sat_invalid_tipo(mock_db):
    event = {
        "pathParameters": {"tipoBusqueda": "invalido"},
        "queryStringParameters": {"q": "prueba"},
        "requestContext": {
            "authorizer": {
                "claims": {"custom:tenant_id": "taller_test"}
            }
        }
    }

    response = search_sat_catalogos_handler(event, None)
    assert response['statusCode'] == 400

def test_search_sat_empty_or_short_query(mock_db):
    # Seed the mock database for unidad
    db = mock_db["_platform"]
    db.unidad.insert_one({"clave": "H87", "descripcion": "Pieza"})

    # Short query (1 char)
    event = {
        "pathParameters": {"tipoBusqueda": "unidad"},
        "queryStringParameters": {"q": "p"},
        "requestContext": {
            "authorizer": {
                "claims": {"custom:tenant_id": "taller_test"}
            }
        }
    }

    response = search_sat_catalogos_handler(event, None)
    assert response['statusCode'] == 200
    data = json.loads(response['body'])['data']
    assert len(data) == 0
