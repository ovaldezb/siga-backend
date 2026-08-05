import json
from src.handlers.clientes.clientes_manager import (
    create_cliente_handler, 
    list_clientes_handler, 
    add_vehiculo_handler,
    update_cliente_handler
)

def test_create_cliente_with_apellidos(mock_db):
    """Verifica que el cliente se cree con nombre y apellidos separados."""
    event = {
        "body": json.dumps({
            "sucursal_id": "sucursal123",
            "nombre": "Juan",
            "apellido_paterno": "Perez",
            "apellido_materno": "Lopez",
            "telefono": "5512345678",
            "email": "juan@example.com",
            "direccion": "Calle Falsa 123"
        }),
        "requestContext": {"authorizer": {"claims": {"custom:tenant_id": "tallertest"}}}
    }
    
    response = create_cliente_handler(event, None)
    assert response['statusCode'] == 201
    
    data = json.loads(response['body'])['data']
    assert data['nombre'] == "Juan"
    assert data['apellido_paterno'] == "Perez"
    assert data['apellido_materno'] == "Lopez"
    assert "vehiculos_resumen" in data
    assert data['vehiculos_resumen'] == []

from bson import ObjectId

def test_add_vehiculo_to_cliente_and_summary(mock_db):
    """Verifica que al añadir un vehículo, este aparezca en el resumen del cliente."""
    tenant_id = "tallertest"
    db = mock_db[f"t_{tenant_id}"]
    
    # 1. Crear cliente previo (usando ObjectId real para simular DB)
    cliente_res = db.clientes.insert_one({
        "nombre": "Pedro",
        "apellido_paterno": "Picapiedra",
        "vehiculos_resumen": [],
        "tenant_id": tenant_id
    })
    cliente_id = str(cliente_res.inserted_id)
    
    # 2. Añadir vehículo
    event = {
        "pathParameters": {"id": cliente_id},
        "body": json.dumps({
            "sucursal_id": "sucursal123",
            "marca": "Toyota",
            "modelo": "Corolla",
            "año": 2022,
            "placas": "ABC-123",
            "vin": "VIN123456789",
            "color": "Blanco"
        }),
        "requestContext": {"authorizer": {"claims": {"custom:tenant_id": tenant_id}}}
    }
    
    response = add_vehiculo_handler(event, None)
    assert response['statusCode'] == 201
    
    # 3. Verificar en DB el resumen del cliente
    cliente_updated = db.clientes.find_one({"_id": ObjectId(cliente_id)})
    resumen = cliente_updated['vehiculos_resumen']
    assert len(resumen) == 1
    assert resumen[0]['placas'] == "ABC-123"
    assert resumen[0]['marca'] == "Toyota"
    
    # 4. Verificar que se creó en la colección global de vehículos
    vehiculo_global = db.vehiculos.find_one({"placas": "ABC-123"})
    assert vehiculo_global is not None
    assert vehiculo_global['cliente_id'] == cliente_id

def test_create_and_update_cliente_with_uso_cfdi(mock_db):
    """Verifica la persistencia y actualización del campo uso_cfdi en clientes."""
    # 1. Crear
    event = {
        "body": json.dumps({
            "sucursal_id": "sucursal123",
            "nombre": "Pedro",
            "apellido_paterno": "Pica",
            "uso_cfdi": "G03"
        }),
        "requestContext": {"authorizer": {"claims": {"custom:tenant_id": "tallertest"}}}
    }
    response = create_cliente_handler(event, None)
    assert response['statusCode'] == 201
    
    data = json.loads(response['body'])['data']
    assert data['uso_cfdi'] == "G03"
    cliente_id = data['id']

    # 2. Actualizar
    event_update = {
        "pathParameters": {"id": cliente_id},
        "body": json.dumps({
            "uso_cfdi": "I08"
        }),
        "requestContext": {"authorizer": {"claims": {"custom:tenant_id": "tallertest"}}}
    }
    response_update = update_cliente_handler(event_update, None)
    assert response_update['statusCode'] == 200
    
    data_update = json.loads(response_update['body'])['data']
    assert data_update['uso_cfdi'] == "I08"
