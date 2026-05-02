import json
from src.handlers.inventario.items_manager import create_item_handler, update_stock_handler

def test_create_producto_success(mock_db):
    """Prueba la creación exitosa de un producto con stock."""
    event = {
        "body": json.dumps({
            "tipo": "PRODUCTO",
            "codigo": "ACE-01",
            "nombre": "Aceite Sintético",
            "precio_venta": 250,
            "stock": 10,
            "maneja_inventario": True
        }),
        "requestContext": {
            "authorizer": {
                "claims": {"custom:tenant_id": "taller_test"}
            }
        }
    }
    
    response = create_item_handler(event, None)
    assert response['statusCode'] == 201
    
    data = json.loads(response['body'])['data']
    assert data['tipo'] == "PRODUCTO"
    assert data['stock'] == 10
    assert data['maneja_inventario'] == True

def test_create_servicio_forces_no_inventory(mock_db):
    """Prueba que los servicios ignoren el stock y maneja_inventario."""
    event = {
        "body": json.dumps({
            "tipo": "SERVICIO",
            "codigo": "MANO-01",
            "nombre": "Limpieza Inyectores",
            "precio_venta": 800,
            "stock": 999, # Debe ser ignorado
            "maneja_inventario": True # Debe ser ignorado
        }),
        "requestContext": {
            "authorizer": {
                "claims": {"custom:tenant_id": "taller_test"}
            }
        }
    }
    
    response = create_item_handler(event, None)
    assert response['statusCode'] == 201
    
    data = json.loads(response['body'])['data']
    assert data['tipo'] == "SERVICIO"
    assert data['maneja_inventario'] == False
    assert data['stock'] is None

def test_update_stock_incremental(mock_db):
    """Prueba el ajuste diferencial de stock (+n / -n)."""
    # 1. Crear producto base
    db = mock_db["t_taller_test"]
    item_res = db.items.insert_one({
        "tipo": "PRODUCTO",
        "codigo": "FIL-01",
        "nombre": "Filtro",
        "stock": 10,
        "maneja_inventario": True,
        "tenant_id": "taller_test"
    })
    item_id = str(item_res.inserted_id)
    
    # 2. Ajuste positivo (+5)
    event_add = {
        "pathParameters": {"id": item_id},
        "body": json.dumps({"cantidad": 5}),
        "requestContext": {"authorizer": {"claims": {"custom:tenant_id": "taller_test"}}}
    }
    resp_add = update_stock_handler(event_add, None)
    assert json.loads(resp_add['body'])['data']['nuevo_stock'] == 15
    
    # 3. Ajuste negativo (-2)
    event_sub = {
        "pathParameters": {"id": item_id},
        "body": json.dumps({"cantidad": -2}),
        "requestContext": {"authorizer": {"claims": {"custom:tenant_id": "taller_test"}}}
    }
    resp_sub = update_stock_handler(event_sub, None)
    assert json.loads(resp_sub['body'])['data']['nuevo_stock'] == 13
