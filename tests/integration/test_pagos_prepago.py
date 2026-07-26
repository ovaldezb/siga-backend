import json
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock
from src.handlers.admin.pagos_manager import procesar_pago_suscripcion_handler

def test_procesar_pago_prepago_inicial(mock_db):
    """Verifica que el primer pago de pre-pago (con gracia) no avance el corte, pero sí la fecha de pago."""
    db_platform = mock_db["_platform"]
    
    # 1. Insertar un taller con fechas de primer pago (gracia)
    # Corte en 1 mes, Pago en 10 días
    tenant_id = "test-tenant-123"
    taller_doc = {
        "tenantId": tenant_id,
        "nombreComercial": "Taller Test",
        "proximaFechaCorte": datetime(2026, 2, 1),
        "proximaFechaPago": datetime(2026, 1, 11),
        "mesesCargo": 1,
        "estado": "ACTIVO"
    }
    db_platform.talleres.insert_one(taller_doc)

    # Mock de la respuesta de Clip API
    mock_response = MagicMock()
    mock_response.__enter__.return_value = mock_response
    mock_response.read.return_value = json.dumps({
        "status": "APPROVED",
        "id": "clip-folio-123",
        "payment_method": {
            "brand": "visa",
            "last4": "4321"
        }
    }).encode('utf-8')

    # Mock de Cognito claims y urllib request
    event = {
        "requestContext": {
            "authorizer": {
                "claims": {
                    "custom:tenant_id": tenant_id,
                    "email": "admin@test.com",
                    "cognito:groups": "ADMIN"
                }
            }
        },
        "body": json.dumps({
            "card_token_id": "tok_123",
            "monto": "150.00",
            "concepto": "Suscripción"
        })
    }

    # Fijar la fecha actual de pago a 2026-01-05 (dentro del periodo de gracia)
    with patch('urllib.request.urlopen', return_value=mock_response), \
         patch('src.handlers.admin.pagos_manager.datetime') as mock_datetime:
        
        # Simular que "hoy" es 2026-01-05
        mock_datetime.utcnow.return_value = datetime(2026, 1, 5)
        # Asegurarse de que datetime() normal funcione
        mock_datetime.side_effect = lambda *args, **kw: datetime(*args, **kw)
        
        response = procesar_pago_suscripcion_handler(event, None)
        assert response['statusCode'] == 200

    # Verificar en la base de datos
    updated_taller = db_platform.talleres.find_one({"tenantId": tenant_id})
    # Al ser el primer pago y estar a tiempo (pago_dt < corte_dt):
    # - nueva_corte debe quedarse en 2026-02-01
    # - nueva_pago debe actualizarse a 2026-02-01
    assert updated_taller["proximaFechaCorte"] == datetime(2026, 2, 1)
    assert updated_taller["proximaFechaPago"] == datetime(2026, 2, 1)

def test_procesar_pago_prepago_recurrente(mock_db):
    """Verifica que un pago recurrente avance tanto el corte como la fecha de pago por los mesesCargo."""
    db_platform = mock_db["_platform"]
    
    # 1. Insertar un taller listo para renovación recurrente (pago == corte)
    tenant_id = "test-tenant-456"
    taller_doc = {
        "tenantId": tenant_id,
        "nombreComercial": "Taller Test Recurrente",
        "proximaFechaCorte": datetime(2026, 2, 1),
        "proximaFechaPago": datetime(2026, 2, 1),
        "mesesCargo": 3, # Trimestral
        "estado": "ACTIVO"
    }
    db_platform.talleres.insert_one(taller_doc)

    mock_response = MagicMock()
    mock_response.__enter__.return_value = mock_response
    mock_response.read.return_value = json.dumps({
        "status": "APPROVED",
        "id": "clip-folio-456",
        "payment_method": {
            "brand": "mastercard",
            "last4": "8888"
        }
    }).encode('utf-8')

    event = {
        "requestContext": {
            "authorizer": {
                "claims": {
                    "custom:tenant_id": tenant_id,
                    "email": "admin@test.com",
                    "cognito:groups": "ADMIN"
                }
            }
        },
        "body": json.dumps({
            "card_token_id": "tok_456",
            "monto": "450.00"
        })
    }

    # Pagar a tiempo el 2026-02-01 (mismo día de corte/pago)
    with patch('urllib.request.urlopen', return_value=mock_response), \
         patch('src.handlers.admin.pagos_manager.datetime') as mock_datetime:
        
        mock_datetime.utcnow.return_value = datetime(2026, 2, 1)
        mock_datetime.side_effect = lambda *args, **kw: datetime(*args, **kw)
        
        response = procesar_pago_suscripcion_handler(event, None)
        assert response['statusCode'] == 200

    updated_taller = db_platform.talleres.find_one({"tenantId": tenant_id})
    # Al ser recurrente y a tiempo:
    # - nueva_corte = 2026-02-01 + 3 meses = 2026-05-01
    # - nueva_pago = 2026-05-01
    assert updated_taller["proximaFechaCorte"] == datetime(2026, 5, 1)
    assert updated_taller["proximaFechaPago"] == datetime(2026, 5, 1)

def test_procesar_pago_prepago_tardio(mock_db):
    """Verifica que un pago tardío reactive la suscripción a partir de la fecha de pago."""
    db_platform = mock_db["_platform"]
    
    tenant_id = "test-tenant-789"
    taller_doc = {
        "tenantId": tenant_id,
        "nombreComercial": "Taller Test Tardio",
        "proximaFechaCorte": datetime(2026, 2, 1),
        "proximaFechaPago": datetime(2026, 2, 1),
        "mesesCargo": 1,
        "estado": "ACTIVO"
    }
    db_platform.talleres.insert_one(taller_doc)

    mock_response = MagicMock()
    mock_response.__enter__.return_value = mock_response
    mock_response.read.return_value = json.dumps({
        "status": "APPROVED",
        "id": "clip-folio-789",
        "payment_method": {
            "brand": "amex",
            "last4": "9999"
        }
    }).encode('utf-8')

    event = {
        "requestContext": {
            "authorizer": {
                "claims": {
                    "custom:tenant_id": tenant_id,
                    "email": "admin@test.com",
                    "cognito:groups": "ADMIN"
                }
            }
        },
        "body": json.dumps({
            "card_token_id": "tok_789",
            "monto": "150.00"
        })
    }

    # Pagar tarde el 2026-02-15 (después del vencimiento 2026-02-01)
    with patch('urllib.request.urlopen', return_value=mock_response), \
         patch('src.handlers.admin.pagos_manager.datetime') as mock_datetime:
        
        mock_datetime.utcnow.return_value = datetime(2026, 2, 15)
        mock_datetime.side_effect = lambda *args, **kw: datetime(*args, **kw)
        
        response = procesar_pago_suscripcion_handler(event, None)
        assert response['statusCode'] == 200

    updated_taller = db_platform.talleres.find_one({"tenantId": tenant_id})
    # Al ser tardío (fecha_pago > corte_dt):
    # - nueva_corte = 2026-02-15 + 1 mes = 2026-03-15
    # - nueva_pago = 2026-03-15
    assert updated_taller["proximaFechaCorte"] == datetime(2026, 3, 15)
    assert updated_taller["proximaFechaPago"] == datetime(2026, 3, 15)
