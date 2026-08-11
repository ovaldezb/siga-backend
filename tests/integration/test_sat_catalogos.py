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

    # 1. Test happy path start-with match
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

    # 2. Test that it does NOT match when query is in the middle of description
    event2 = {
        "pathParameters": {"tipoBusqueda": "unidad"},
        "queryStringParameters": {"q": "cisterna"},
        "requestContext": {
            "authorizer": {
                "claims": {"custom:tenant_id": "taller_test"}
            }
        }
    }
    response2 = search_sat_catalogos_handler(event2, None)
    assert response2['statusCode'] == 200
    data2 = json.loads(response2['body'])['data']
    assert len(data2) == 0  # Should be empty because it starts with "Camión", not "cisterna"

    # 3. Test that it matches when query is the start of the description
    event3 = {
        "pathParameters": {"tipoBusqueda": "unidad"},
        "queryStringParameters": {"q": "camión"},
        "requestContext": {
            "authorizer": {
                "claims": {"custom:tenant_id": "taller_test"}
            }
        }
    }
    response3 = search_sat_catalogos_handler(event3, None)
    assert response3['statusCode'] == 200
    data3 = json.loads(response3['body'])['data']
    assert len(data3) == 1
    assert data3[0]['clave'] == "19"

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

def test_search_sat_regimenfiscal(mock_db):
    db = mock_db["_platform"]
    db.regimen_fiscal.insert_many([
        {"regimenfiscal": "601", "descripcion": "General de Ley Personas Morales", "fisica": False, "moral": True},
        {"regimenfiscal": "612", "descripcion": "Personas Físicas con Actividades Empresariales", "fisica": True, "moral": False}
    ])

    # Test query without parameter (should return all)
    event = {
        "pathParameters": {"tipoBusqueda": "regimenfiscal"},
        "queryStringParameters": {},
        "requestContext": {
            "authorizer": {
                "claims": {"custom:tenant_id": "taller_test"}
            }
        }
    }

    response = search_sat_catalogos_handler(event, None)
    assert response['statusCode'] == 200
    data = json.loads(response['body'])['data']
    assert len(data) == 2
    assert data[0]['clave'] == "601"
    assert data[0]['descripcion'] == "General de Ley Personas Morales"
    assert data[0]['fisica'] is False
    assert data[0]['moral'] is True
    assert data[1]['clave'] == "612"

    # Test query with description filter
    event["queryStringParameters"] = {"q": "Físicas"}
    response = search_sat_catalogos_handler(event, None)
    assert response['statusCode'] == 200
    data = json.loads(response['body'])['data']
    assert len(data) == 1
    assert data[0]['clave'] == "612"
