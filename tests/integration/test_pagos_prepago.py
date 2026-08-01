import json
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock
from src.handlers.admin.pagos_manager import procesar_pago_suscripcion_handler

class MetaDatetime(type):
    def __instancecheck__(cls, inst):
        return isinstance(inst, datetime)

class FakeDatetime(datetime, metaclass=MetaDatetime):
    _mock_utcnow = None
    
    @classmethod
    def utcnow(cls):
        return cls._mock_utcnow or datetime.utcnow()

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
    FakeDatetime._mock_utcnow = datetime(2026, 1, 5)
    with patch('urllib.request.urlopen', return_value=mock_response), \
         patch('src.handlers.admin.pagos_manager.datetime', FakeDatetime):
        
        response = procesar_pago_suscripcion_handler(event, None)
        assert response['statusCode'] == 200

    # Verificar en la base de datos
    updated_taller = db_platform.talleres.find_one({"tenantId": tenant_id})
    # Al ser el primer pago y estar a tiempo (pago_dt < corte_dt):
    # - nueva_corte debe quedarse en 2026-02-01
    # - nueva_pago debe ser 10 días después del corte (2026-02-11)
    assert updated_taller["proximaFechaCorte"] == datetime(2026, 2, 1)
    assert updated_taller["proximaFechaPago"] == datetime(2026, 2, 11)

def test_procesar_pago_prepago_recurrente(mock_db):
    """Verifica que un pago recurrente avance tanto el corte como la fecha de pago por los mesesCargo."""
    db_platform = mock_db["_platform"]
    
    # 1. Insertar un taller listo para renovación recurrente
    tenant_id = "test-tenant-456"
    taller_doc = {
        "tenantId": tenant_id,
        "nombreComercial": "Taller Test Recurrente",
        "proximaFechaCorte": datetime(2026, 2, 1),
        "proximaFechaPago": datetime(2026, 2, 11),
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

    # Pagar a tiempo el 2026-02-01 (antes del vencimiento 2026-02-11)
    FakeDatetime._mock_utcnow = datetime(2026, 2, 1)
    with patch('urllib.request.urlopen', return_value=mock_response), \
         patch('src.handlers.admin.pagos_manager.datetime', FakeDatetime):
        
        response = procesar_pago_suscripcion_handler(event, None)
        assert response['statusCode'] == 200

    updated_taller = db_platform.talleres.find_one({"tenantId": tenant_id})
    # Al ser recurrente y a tiempo:
    # - nueva_corte = 2026-02-01 + 3 meses = 2026-05-01
    # - nueva_pago = 2026-05-01 + 10 días = 2026-05-11
    assert updated_taller["proximaFechaCorte"] == datetime(2026, 5, 1)
    assert updated_taller["proximaFechaPago"] == datetime(2026, 5, 11)

def test_procesar_pago_prepago_tardio(mock_db):
    """Verifica que un pago tardío reactive la suscripción a partir de la fecha de pago."""
    db_platform = mock_db["_platform"]
    
    tenant_id = "test-tenant-789"
    taller_doc = {
        "tenantId": tenant_id,
        "nombreComercial": "Taller Test Tardio",
        "proximaFechaCorte": datetime(2026, 2, 1),
        "proximaFechaPago": datetime(2026, 2, 11),
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

    # Pagar tarde el 2026-02-15 (después del vencimiento 2026-02-11)
    FakeDatetime._mock_utcnow = datetime(2026, 2, 15)
    with patch('urllib.request.urlopen', return_value=mock_response), \
         patch('src.handlers.admin.pagos_manager.datetime', FakeDatetime):
        
        response = procesar_pago_suscripcion_handler(event, None)
        assert response['statusCode'] == 200

    updated_taller = db_platform.talleres.find_one({"tenantId": tenant_id})
    # Al ser tardío (fecha_pago > pago_dt, en este caso 2026-02-15 > 2026-02-11):
    # - corte inicial = 2026-02-01
    # - nueva_corte = corte_dt + 1 mes - 10 días = Mar 1 - 10 días = Feb 19
    # - nueva_pago = Feb 19 + 10 días = Mar 1
    assert updated_taller["proximaFechaCorte"] == datetime(2026, 2, 19)
    assert updated_taller["proximaFechaPago"] == datetime(2026, 3, 1)
