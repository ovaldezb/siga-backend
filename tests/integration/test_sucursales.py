import json
from bson import ObjectId
from src.handlers.sucursales.sucursales_manager import (
    list_sucursales_handler,
    create_sucursal_handler,
    update_sucursal_handler,
    delete_sucursal_handler,
    get_sucursal_handler
)

TENANT = "tallertest"

def _claims():
    return {"custom:tenant_id": TENANT, "sub": "user-1", "email": "test@taller.com", "name": "Tester"}

def test_sucursal_crud_flow(mock_db):
    db = mock_db[f"t_{TENANT}"]
    
    # 1. Create Sucursal
    event_create = {
        "body": json.dumps({
            "nombre": "Sucursal Poniente",
            "direccion": "Av. Principal 123",
            "telefono": "5551234",
            "responsable": "David",
            "serie": "B",
            "codigo_postal": "54321"
        }),
        "requestContext": {"authorizer": {"claims": _claims()}}
    }
    
    resp_create = create_sucursal_handler(event_create, None)
    assert resp_create["statusCode"] == 201
    
    sucursal_id = json.loads(resp_create["body"])["data"]["id"]
    
    # 2. Get Sucursal by ID
    event_get = {
        "pathParameters": {"id": sucursal_id},
        "requestContext": {"authorizer": {"claims": _claims()}}
    }
    
    resp_get = get_sucursal_handler(event_get, None)
    assert resp_get["statusCode"] == 200
    
    data_get = json.loads(resp_get["body"])["data"]
    assert data_get["nombre"] == "Sucursal Poniente"
    assert data_get["serie"] == "B"
    assert data_get["codigo_postal"] == "54321"

    # 3. List Sucursales
    event_list = {
        "requestContext": {"authorizer": {"claims": _claims()}}
    }
    
    resp_list = list_sucursales_handler(event_list, None)
    assert resp_list["statusCode"] == 200
    assert len(json.loads(resp_list["body"])["data"]) == 1

    # 4. Update Sucursal
    event_update = {
        "pathParameters": {"id": sucursal_id},
        "body": json.dumps({
            "nombre": "Sucursal Poniente Editada"
        }),
        "requestContext": {"authorizer": {"claims": _claims()}}
    }
    
    resp_update = update_sucursal_handler(event_update, None)
    assert resp_update["statusCode"] == 200
    assert json.loads(resp_update["body"])["data"]["nombre"] == "Sucursal Poniente Editada"

    # 5. Delete Sucursal
    resp_delete = delete_sucursal_handler(event_get, None)
    assert resp_delete["statusCode"] == 200
