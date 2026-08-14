import json
from unittest.mock import patch, MagicMock
from datetime import datetime
from bson import ObjectId
from src.handlers.admin.talleres_manager import create_taller_handler
from src.handlers.admin.pagos_manager import procesar_pago_suscripcion_handler, openpay_webhook_handler

def test_create_taller_with_openpay(mock_db):
    """Verifica que al crear un taller se asigne correctamente la cuenta CLABE de Openpay."""
    event = {
        "body": json.dumps({
            "nombreComercial": "Auto Service Openpay",
            "adminEmail": "admin_openpay@test.com",
            "adminNombre": "Juan",
            "adminApellido": "Perez",
            "precioSuscripcion": 599.00
        })
    }
    
    mock_cust = {"id": "cus_123456789"}
    mock_spei = {
        "id": "tr_987654321",
        "payment_method": {
            "clabe": "012345678901234567",
            "bank": "STP"
        }
    }

    with patch('src.handlers.admin.talleres_manager.client') as mock_cognito, \
         patch('src.shared.utils.openpay_client.create_customer', return_value=mock_cust) as mock_create_cust, \
         patch('src.shared.utils.openpay_client.create_spei_charge', return_value=mock_spei) as mock_create_spei:
             
        response = create_taller_handler(event, None)
        assert response['statusCode'] == 201
        
        data = json.loads(response['body'])['data']
        assert data['openpayCustomerId'] == "cus_123456789"
        assert data['openpayClabe'] == "012345678901234567"
        assert data['openpaySpeiChargeId'] == "tr_987654321"

def test_procesar_pago_suscripcion_openpay_card(mock_db):
    """Verifica el procesamiento exitoso de cargo a tarjeta vía Openpay."""
    db_platform = mock_db["_platform"]
    db_platform.talleres.insert_one({
        "tenantId": "taller_op_card",
        "adminEmail": "op_card@test.com",
        "adminNombre": "Juan",
        "adminApellido": "Perez",
        "openpayCustomerId": "cus_card_123",
        "proximaFechaCorte": datetime(2026, 1, 1),
        "proximaFechaPago": datetime(2026, 1, 11)
    })

    event = {
        "body": json.dumps({
            "openpay_token_id": "tok_card_456",
            "device_session_id": "dev_sess_789",
            "monto": 599.00,
            "concepto": "Suscripción Mensual Mekanics Manager"
        }),
        "requestContext": {
            "authorizer": {
                "claims": {
                    "custom:tenant_id": "taller_op_card",
                    "email": "op_card@test.com",
                    "cognito:groups": ["ADMIN"]
                }
            }
        }
    }

    mock_charge = {
        "id": "tr_card_999",
        "card": {
            "brand": "visa",
            "card_number": "411111XXXXXX1111"
        }
    }

    with patch('src.shared.utils.openpay_client.create_card_charge', return_value=mock_charge) as mock_card_charge:
        response = procesar_pago_suscripcion_handler(event, None)
        assert response['statusCode'] == 200
        
        pago = db_platform.suscripciones_pagos.find_one({"tallerTenantId": "taller_op_card"})
        assert pago is not None
        assert pago["estado"] == "COMPLETADO"
        assert pago["metodo"] == "Tarjeta (Openpay - VISA •••• 1111)"
        
        taller = db_platform.talleres.find_one({"tenantId": "taller_op_card"})
        assert taller["estado"] == "ACTIVO"
        assert taller["proximaFechaCorte"] > datetime(2026, 1, 1)

def test_openpay_spei_webhook_success(mock_db):
    """Verifica que el webhook de SPEI procese correctamente la confirmación de depósito de Openpay."""
    db_platform = mock_db["_platform"]
    db_platform.talleres.insert_one({
        "tenantId": "taller_webhook",
        "adminEmail": "webhook@test.com",
        "openpayCustomerId": "cus_webhook_999",
        "openpaySpeiChargeId": "tr_spei_111",
        "openpayClabe": "012345678901234567",
        "precioSuscripcion": 599.00,
        "proximaFechaCorte": datetime(2026, 1, 1),
        "proximaFechaPago": datetime(2026, 1, 11)
    })

    event = {
        "body": json.dumps({
            "type": "charge.succeeded",
            "transaction": {
                "id": "tr_spei_111",
                "customer_id": "cus_webhook_999",
                "amount": 599.00,
                "status": "completed",
                "description": "Suscripcion Mensual Mekanics Manager - Taller"
            }
        })
    }

    mock_new_spei = {
        "id": "tr_spei_222",
        "payment_method": {
            "clabe": "012345678901234567"
        }
    }

    with patch('src.shared.utils.openpay_client.create_spei_charge', return_value=mock_new_spei) as mock_create_spei:
        response = openpay_webhook_handler(event, None)
        assert response['statusCode'] == 200
        
        pago = db_platform.suscripciones_pagos.find_one({"tallerTenantId": "taller_webhook"})
        assert pago is not None
        assert pago["estado"] == "COMPLETADO"
        assert pago["metodo"] == "Transferencia SPEI (Openpay)"
        
        taller = db_platform.talleres.find_one({"tenantId": "taller_webhook"})
        assert taller["openpaySpeiChargeId"] == "tr_spei_222" # Se generó el siguiente cargo SPEI
