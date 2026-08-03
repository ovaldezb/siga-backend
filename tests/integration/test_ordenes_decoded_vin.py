import json
from bson import ObjectId
from src.handlers.ordenes.ordenes_manager import create_orden_handler, update_orden_handler

TENANT = "taller_test"

def test_create_and_update_orden_with_decoded_vin(mock_db):
    db = mock_db[f"t_{TENANT}"]
    
    # 1. Crear un cliente ficticio y vehículo ficticio
    cliente_id = str(ObjectId())
    db.clientes.insert_one({
        "_id": ObjectId(cliente_id),
        "nombre": "Juan Pérez",
        "telefono": "1234567890",
        "email": "juan@test.com"
    })
    
    vehiculo_id = str(ObjectId())
    db.vehiculos.insert_one({
        "_id": ObjectId(vehiculo_id),
        "cliente_id": cliente_id,
        "tenant_id": TENANT,
        "placas": "ABC-1234",
        "marca": "FORD",
        "modelo": "MUSTANG",
        "anio": 2017
    })
    
    # Evento para crear una orden con decodedVin
    create_event = {
        "body": json.dumps({
            "sucursalId": "60c72b2f9b1d8e2568cf4567",
            "estado": "RECEPCION",
            "cliente_snapshot": {"id": cliente_id, "nombre": "Juan Pérez"},
            "vehiculo_id": vehiculo_id,
            "vehiculo_snapshot": {
                "placas": "ABC-1234",
                "marca": "FORD",
                "modelo": "MUSTANG",
                "anio": 2017
            },
            "decodedVin": [
                {"key": "Make", "value": "FORD"},
                {"key": "Model", "value": "MUSTANG"}
            ]
        }),
        "requestContext": {"authorizer": {"claims": {"custom:tenant_id": TENANT, "email": "asesor@test.com"}}}
    }
    
    response = create_orden_handler(create_event, None)
    assert response["statusCode"] == 201, response["body"]
    body = json.loads(response["body"])
    orden_id = body["data"]["id"]
    
    # Verificar en la base de datos que se haya guardado decodedVin
    orden_db = db["ordenes_servicio"].find_one({"_id": ObjectId(orden_id)})
    assert orden_db is not None
    assert orden_db["decodedVin"] == [
        {"key": "Make", "value": "FORD"},
        {"key": "Model", "value": "MUSTANG"}
    ]
    
    # 2. Actualizar la orden usando update_orden_handler con decodedVin
    update_event = {
        "pathParameters": {"id": orden_id},
        "body": json.dumps({
            "decodedVin": [
                {"key": "Make", "value": "FORD"},
                {"key": "Model", "value": "MUSTANG"},
                {"key": "ModelYear", "value": "2020"}
            ]
        }),
        "requestContext": {"authorizer": {"claims": {"custom:tenant_id": TENANT, "email": "asesor@test.com"}}}
    }
    
    update_response = update_orden_handler(update_event, None)
    assert update_response["statusCode"] == 200, update_response["body"]
    
    # Verificar en la base de datos que se haya actualizado a decodedVin
    orden_db_updated = db["ordenes_servicio"].find_one({"_id": ObjectId(orden_id)})
    assert orden_db_updated is not None
    assert orden_db_updated["decodedVin"] == [
        {"key": "Make", "value": "FORD"},
        {"key": "Model", "value": "MUSTANG"},
        {"key": "ModelYear", "value": "2020"}
    ]
