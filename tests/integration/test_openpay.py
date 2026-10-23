import json
from unittest.mock import patch, MagicMock
from datetime import datetime
from bson import ObjectId
from src.handlers.admin.talleres_manager import create_taller_handler
from src.handlers.admin.pagos_manager import (
    procesar_pago_suscripcion_handler,
    openpay_webhook_handler,
    confirmar_pago_openpay_handler
)

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
            "bank": "BBVA Bancomer",
            "agreement": "1422286",
            "name": "00406475954759329265"
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
        assert data['openpayBank'] == "BBVA Bancomer"
        assert data['openpayAgreement'] == "1422286"
        assert data['openpayReference'] == "00406475954759329265"
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
            "clabe": "012345678901234567",
            "bank": "BBVA Bancomer",
            "agreement": "1422286",
            "name": "00406475954759329265"
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
        assert taller["openpayBank"] == "BBVA Bancomer"
        assert taller["openpayAgreement"] == "1422286"
        assert taller["openpayReference"] == "00406475954759329265"

def test_procesar_pago_suscripcion_openpay_3ds_pending(mock_db):
    """Verifica que si Openpay responde charge_pending con url 3DS, se guarde PENDIENTE y retorne la url."""
    db_platform = mock_db["_platform"]
    fecha_corte_orig = datetime(2026, 1, 1)
    db_platform.talleres.insert_one({
        "tenantId": "taller_op_3ds",
        "adminEmail": "op_3ds@test.com",
        "openpayCustomerId": "cus_3ds_123",
        "proximaFechaCorte": fecha_corte_orig,
        "proximaFechaPago": datetime(2026, 1, 11)
    })

    event = {
        "body": json.dumps({
            "openpay_token_id": "tok_3ds_456",
            "device_session_id": "dev_3ds_789",
            "monto": 599.00,
            "concepto": "Suscripción Mensual Mekanics Manager",
            "redirect_url": "https://app.test.com/#/pagos"
        }),
        "requestContext": {
            "authorizer": {
                "claims": {
                    "custom:tenant_id": "taller_op_3ds",
                    "email": "op_3ds@test.com",
                    "cognito:groups": ["ADMIN"]
                }
            }
        }
    }

    mock_charge = {
        "id": "tr_3ds_pending_001",
        "status": "charge_pending",
        "payment_method": {
            "type": "redirect",
            "url": "https://sandbox-api.openpay.mx/v1/3ds/auth/tr_3ds_pending_001"
        },
        "card": {
            "brand": "visa",
            "card_number": "411111XXXXXX1111"
        }
    }

    with patch('src.shared.utils.openpay_client.create_card_charge', return_value=mock_charge) as mock_card_charge:
        response = procesar_pago_suscripcion_handler(event, None)
        assert response['statusCode'] == 200
        data = json.loads(response['body'])['data']
        assert data['requires_3ds'] is True
        assert data['url_3ds'] == "https://sandbox-api.openpay.mx/v1/3ds/auth/tr_3ds_pending_001"
        assert data['charge_id'] == "tr_3ds_pending_001"

        # Verificar que el pago se guardó como PENDIENTE
        pago = db_platform.suscripciones_pagos.find_one({"folioClip": "tr_3ds_pending_001"})
        assert pago is not None
        assert pago["estado"] == "PENDIENTE"

        # Verificar que la vigencia del taller NO se extendió aún
        taller = db_platform.talleres.find_one({"tenantId": "taller_op_3ds"})
        assert taller["proximaFechaCorte"] == fecha_corte_orig

def test_confirmar_pago_openpay_success(mock_db):
    """Verifica que el endpoint de confirmación actualice el pago a COMPLETADO y extienda la vigencia."""
    db_platform = mock_db["_platform"]
    db_platform.talleres.insert_one({
        "tenantId": "taller_confirmar",
        "adminEmail": "confirm@test.com",
        "proximaFechaCorte": datetime(2026, 1, 1),
        "proximaFechaPago": datetime(2026, 1, 11)
    })
    db_platform.suscripciones_pagos.insert_one({
        "tallerTenantId": "taller_confirmar",
        "usuarioEmail": "confirm@test.com",
        "monto": 599.00,
        "concepto": "Suscripción Mensual Mekanics Manager",
        "folioClip": "tr_confirm_999",
        "estado": "PENDIENTE",
        "metodo": "Tarjeta (Openpay - VISA •••• 1111)",
        "fechaPago": datetime(2026, 1, 1)
    })

    event = {
        "body": json.dumps({"charge_id": "tr_confirm_999"}),
        "requestContext": {
            "authorizer": {
                "claims": {
                    "custom:tenant_id": "taller_confirmar",
                    "email": "confirm@test.com",
                    "cognito:groups": ["ADMIN"]
                }
            }
        }
    }

    mock_charge = {
        "id": "tr_confirm_999",
        "status": "completed",
        "amount": 599.00,
        "description": "Suscripción Mensual Mekanics Manager",
        "card": {
            "brand": "visa",
            "card_number": "411111XXXXXX1111"
        }
    }

    with patch('src.shared.utils.openpay_client.get_charge', return_value=mock_charge) as mock_get_charge:
        response = confirmar_pago_openpay_handler(event, None)
        assert response['statusCode'] == 200

        pago = db_platform.suscripciones_pagos.find_one({"folioClip": "tr_confirm_999"})
        assert pago["estado"] == "COMPLETADO"

        taller = db_platform.talleres.find_one({"tenantId": "taller_confirmar"})
        assert taller["estado"] == "ACTIVO"
        assert taller["proximaFechaCorte"] > datetime(2026, 1, 1)

def test_confirmar_pago_openpay_failed(mock_db):
    """Verifica que si Openpay reporta failed, se actualice a FALLIDO y retorne 400."""
    db_platform = mock_db["_platform"]
    db_platform.talleres.insert_one({
        "tenantId": "taller_fail",
        "adminEmail": "fail@test.com"
    })
    db_platform.suscripciones_pagos.insert_one({
        "tallerTenantId": "taller_fail",
        "usuarioEmail": "fail@test.com",
        "folioClip": "tr_fail_888",
        "estado": "PENDIENTE"
    })

    event = {
        "body": json.dumps({"charge_id": "tr_fail_888"}),
        "requestContext": {
            "authorizer": {
                "claims": {
                    "custom:tenant_id": "taller_fail",
                    "email": "fail@test.com",
                    "cognito:groups": ["ADMIN"]
                }
            }
        }
    }

    mock_charge = {
        "id": "tr_fail_888",
        "status": "failed",
        "error_message": "3D Secure authentication failed",
        "error_code": 2010
    }

    with patch('src.shared.utils.openpay_client.get_charge', return_value=mock_charge):
        response = confirmar_pago_openpay_handler(event, None)
        assert response['statusCode'] == 400

        pago = db_platform.suscripciones_pagos.find_one({"folioClip": "tr_fail_888"})
        assert pago["estado"] == "FALLIDO"
        assert "3D Secure authentication failed" in pago["detalle"]

def test_openpay_card_webhook_success(mock_db):
    """Verifica que el webhook procese tarjetas 3DS completadas sin generar nuevo SPEI."""
    db_platform = mock_db["_platform"]
    db_platform.talleres.insert_one({
        "tenantId": "taller_card_wh",
        "adminEmail": "card_wh@test.com",
        "openpayCustomerId": "cus_card_wh_1",
        "openpaySpeiChargeId": "tr_spei_existing",
        "proximaFechaCorte": datetime(2026, 1, 1),
        "proximaFechaPago": datetime(2026, 1, 11)
    })
    db_platform.suscripciones_pagos.insert_one({
        "tallerTenantId": "taller_card_wh",
        "usuarioEmail": "card_wh@test.com",
        "folioClip": "tr_card_wh_3ds",
        "estado": "PENDIENTE"
    })

    event = {
        "body": json.dumps({
            "type": "charge.succeeded",
            "transaction": {
                "id": "tr_card_wh_3ds",
                "customer_id": "cus_card_wh_1",
                "amount": 599.00,
                "status": "completed",
                "method": "card",
                "card": {
                    "brand": "mastercard",
                    "card_number": "555555XXXXXX4444"
                },
                "description": "Suscripcion Mensual Mekanics Manager"
            }
        })
    }

    response = openpay_webhook_handler(event, None)
    assert response['statusCode'] == 200

    pago = db_platform.suscripciones_pagos.find_one({"folioClip": "tr_card_wh_3ds"})
    assert pago["estado"] == "COMPLETADO"
    assert "MASTERCARD" in pago["metodo"]

    taller = db_platform.talleres.find_one({"tenantId": "taller_card_wh"})
    assert taller["proximaFechaCorte"] > datetime(2026, 1, 1)
    # Debe conservar su openpaySpeiChargeId sin generar uno nuevo
    assert taller["openpaySpeiChargeId"] == "tr_spei_existing"


# ── Idempotencia del webhook: un aviso repetido no se procesa dos veces ──────

def _evento_webhook(trans_id, customer_id, method=None, card=None):
    transaction = {
        "id": trans_id,
        "customer_id": customer_id,
        "amount": 599.00,
        "status": "completed",
        "description": "Suscripcion Mensual Mekanics Manager",
    }
    if method:
        transaction["method"] = method
    if card:
        transaction["card"] = card
    return {"body": json.dumps({"type": "charge.succeeded", "transaction": transaction})}


def _taller_spei(db_platform, tenant_id, spei_id, customer_id):
    db_platform.talleres.insert_one({
        "tenantId": tenant_id,
        "adminEmail": f"{tenant_id}@test.com",
        "openpayCustomerId": customer_id,
        "openpaySpeiChargeId": spei_id,
        "precioSuscripcion": 599.00,
        "proximaFechaCorte": datetime(2026, 1, 1),
        "proximaFechaPago": datetime(2026, 1, 11),
    })


_NUEVO_SPEI = {"id": "tr_spei_siguiente", "payment_method": {"clabe": "012", "bank": "BBVA Bancomer", "agreement": "1", "name": "ref"}}


def test_webhook_spei_repetido_se_procesa_una_vez(mock_db):
    db_platform = mock_db["_platform"]
    _taller_spei(db_platform, "taller_rep", "tr_spei_rep", "cus_rep")
    event = _evento_webhook("tr_spei_rep", "cus_rep")

    with patch('src.shared.utils.openpay_client.create_spei_charge', return_value=_NUEVO_SPEI) as mock_spei, \
         patch('src.handlers.admin.pagos_manager._encolar_facturacion') as mock_factura:
        r1 = openpay_webhook_handler(event, None)
        corte_1 = db_platform.talleres.find_one({"tenantId": "taller_rep"})["proximaFechaCorte"]
        r2 = openpay_webhook_handler(event, None)

    assert r1["statusCode"] == 200 and r2["statusCode"] == 200
    assert db_platform.suscripciones_pagos.count_documents({"folioClip": "tr_spei_rep"}) == 1
    assert db_platform.talleres.find_one({"tenantId": "taller_rep"})["proximaFechaCorte"] == corte_1
    assert mock_spei.call_count == 1
    assert mock_factura.call_count == 1
    evento = db_platform.openpay_webhook_eventos.find_one({"_id": "tr_spei_rep"})
    assert evento["estado"] == "PROCESADO" and evento["tenantId"] == "taller_rep"


def test_webhook_tarjeta_repetido_se_procesa_una_vez(mock_db):
    db_platform = mock_db["_platform"]
    db_platform.talleres.insert_one({
        "tenantId": "taller_card_rep", "adminEmail": "c@test.com", "openpayCustomerId": "cus_card_rep",
        "proximaFechaCorte": datetime(2026, 1, 1), "proximaFechaPago": datetime(2026, 1, 11),
    })
    event = _evento_webhook("tr_card_rep", "cus_card_rep", method="card",
                            card={"brand": "visa", "card_number": "411111XXXXXX1111", "type": "debit"})

    with patch('src.handlers.admin.pagos_manager._encolar_facturacion') as mock_factura:
        openpay_webhook_handler(event, None)
        corte_1 = db_platform.talleres.find_one({"tenantId": "taller_card_rep"})["proximaFechaCorte"]
        r2 = openpay_webhook_handler(event, None)

    assert r2["statusCode"] == 200
    assert db_platform.suscripciones_pagos.count_documents({"folioClip": "tr_card_rep"}) == 1
    assert db_platform.talleres.find_one({"tenantId": "taller_card_rep"})["proximaFechaCorte"] == corte_1
    assert mock_factura.call_count == 1


def test_webhook_spei_con_pago_previo_sin_reclamo_no_duplica(mock_db):
    """Pagos registrados antes de existir el reclamo: un reintento no los repite."""
    db_platform = mock_db["_platform"]
    _taller_spei(db_platform, "taller_hist", "tr_spei_hist", "cus_hist")
    db_platform.suscripciones_pagos.insert_one({
        "tallerTenantId": "taller_hist", "folioClip": "tr_spei_hist", "estado": "COMPLETADO",
    })

    with patch('src.shared.utils.openpay_client.create_spei_charge', return_value=_NUEVO_SPEI) as mock_spei, \
         patch('src.handlers.admin.pagos_manager._encolar_facturacion') as mock_factura:
        r = openpay_webhook_handler(_evento_webhook("tr_spei_hist", "cus_hist"), None)

    assert r["statusCode"] == 200
    assert db_platform.suscripciones_pagos.count_documents({"folioClip": "tr_spei_hist"}) == 1
    assert db_platform.talleres.find_one({"tenantId": "taller_hist"})["proximaFechaCorte"] == datetime(2026, 1, 1)
    mock_spei.assert_not_called()
    mock_factura.assert_not_called()


def test_webhook_falla_libera_el_reclamo_y_el_reintento_procesa(mock_db):
    db_platform = mock_db["_platform"]
    _taller_spei(db_platform, "taller_falla", "tr_spei_falla", "cus_falla")
    event = _evento_webhook("tr_spei_falla", "cus_falla")

    with patch('src.handlers.admin.pagos_manager.calcular_nuevas_fechas_suscripcion', side_effect=RuntimeError("falla")), \
         patch('src.shared.utils.openpay_client.create_spei_charge', return_value=_NUEVO_SPEI):
        r1 = openpay_webhook_handler(event, None)
    assert r1["statusCode"] >= 500
    assert db_platform.openpay_webhook_eventos.find_one({"_id": "tr_spei_falla"}) is None
    # El pago insertado antes de la falla se deshace: si no, el reintento lo vería y
    # respondería "ya recibido" sin extender la suscripción.
    assert db_platform.suscripciones_pagos.count_documents({"folioClip": "tr_spei_falla"}) == 0

    with patch('src.shared.utils.openpay_client.create_spei_charge', return_value=_NUEVO_SPEI), \
         patch('src.handlers.admin.pagos_manager._encolar_facturacion') as mock_factura:
        r2 = openpay_webhook_handler(event, None)
    assert r2["statusCode"] == 200
    assert db_platform.suscripciones_pagos.count_documents({"folioClip": "tr_spei_falla"}) == 1
    assert db_platform.talleres.find_one({"tenantId": "taller_falla"})["proximaFechaCorte"] > datetime(2026, 1, 1)
    assert mock_factura.call_count == 1


def test_webhook_sin_taller_no_se_reprocesa(mock_db):
    db_platform = mock_db["_platform"]
    event = _evento_webhook("tr_spei_huerfano", "cus_inexistente")

    with patch('src.shared.utils.openpay_client.create_spei_charge', return_value=_NUEVO_SPEI) as mock_spei:
        r1 = openpay_webhook_handler(event, None)
        r2 = openpay_webhook_handler(event, None)

    assert r1["statusCode"] == 200 and r2["statusCode"] == 200
    assert db_platform.openpay_webhook_eventos.find_one({"_id": "tr_spei_huerfano"})["estado"] == "SIN_TALLER"
    assert db_platform.suscripciones_pagos.count_documents({}) == 0
    mock_spei.assert_not_called()
