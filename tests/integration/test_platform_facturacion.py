import json
from datetime import datetime
from bson import ObjectId
from unittest.mock import patch, MagicMock

from src.handlers.admin.platform_facturacion_manager import (
    get_platform_emisor_config_handler,
    save_platform_emisor_config_handler,
    get_platform_certificates_handler,
    facturar_pago_suscripcion_handler,
    list_platform_facturas_handler,
    get_platform_factura_pdf_handler,
    FACTURA_CLAVE_PROD_SERV,
    FACTURA_CLAVE_UNIDAD,
    FACTURA_DESCRIPCION
)

def _super_admin_claims():
    return {
        "cognito:groups": "SUPER_ADMIN",
        "sub": "superadmin-1",
        "email": "superadmin@mekanicsmanager.com",
        "name": "Super Admin"
    }

def test_platform_emisor_config(mock_db):
    """Verifica guardar y consultar la configuración fiscal del emisor en _platform."""
    # 1. Guardar configuración
    save_event = {
        "body": json.dumps({
            "nombre": "Mekanics Manager Corp",
            "rfc": "VABO780711D41",
            "regimen_fiscal": "612",
            "codigo_postal": "52756",
            "serie": "MM",
            "direccion": "Av. Insurgentes 100",
            "folio_inicial": 10
        }),
        "requestContext": {"authorizer": {"claims": _super_admin_claims()}}
    }
    res_save = save_platform_emisor_config_handler(save_event, None)
    assert res_save["statusCode"] == 200

    # 2. Consultar configuración
    get_event = {
        "requestContext": {"authorizer": {"claims": _super_admin_claims()}}
    }
    res_get = get_platform_emisor_config_handler(get_event, None)
    assert res_get["statusCode"] == 200
    data = json.loads(res_get["body"])["data"]
    assert data["nombre"] == "Mekanics Manager Corp"
    assert data["rfc"] == "VABO780711D41"
    assert data["regimen_fiscal"] == "612"
    assert data["codigo_postal"] == "52756"
    assert data["serie"] == "MM"
    assert data["folio_actual"] == 10

def test_platform_certificates_list(mock_db):
    """Verifica listado de certificados en _platform."""
    db = mock_db["_platform"]
    db["certificates"].insert_one({
        "nombre": "Mekanics Manager",
        "rfc": "VABO780711D41",
        "no_certificado": "00001000000714885049",
        "desde": datetime(2025, 1, 1),
        "hasta": datetime(2029, 1, 1),
        "createdAt": datetime.utcnow()
    })

    event = {
        "requestContext": {"authorizer": {"claims": _super_admin_claims()}}
    }
    res = get_platform_certificates_handler(event, None)
    assert res["statusCode"] == 200
    items = json.loads(res["body"])["data"]
    assert len(items) == 1
    assert items[0]["rfc"] == "VABO780711D41"

def test_facturar_pago_suscripcion_success(mock_db, monkeypatch):
    """Verifica la emisión completa de CFDI 4.0 a un taller por su pago de suscripción."""
    db = mock_db["_platform"]

    # 1. Configurar emisor y CSD en plataforma
    cert_id = db["certificates"].insert_one({
        "nombre": "OMAR VALDEZ BECERRIL",
        "rfc": "VABO780711D41",
        "no_certificado": "00001000000714885049",
        "desde": datetime(2025, 1, 1),
        "hasta": datetime(2029, 1, 1),
        "createdAt": datetime.utcnow()
    }).inserted_id

    db["sucursales"].insert_one({
        "nombre": "OMAR VALDEZ BECERRIL",
        "rfc": "VABO780711D41",
        "regimen_fiscal": "612",
        "codigo_postal": "52756",
        "serie": "F",
        "id_certificado": str(cert_id),
        "is_platform": True
    })

    # 2. Registrar taller con datos fiscales
    tenant_id = "tenant-taller-pro"
    taller_id = db["talleres"].insert_one({
        "nombreComercial": "Auto Service Pro",
        "tenantId": tenant_id,
        "datosFiscales": {
            "rfc": "MUMR870225TM1",
            "razonSocial": "ROBERTO CARLOS MUÑOZ MARQUEZ",
            "codigoPostal": "20030",
            "regimenFiscal": "626",
            "usoCfdi": "G03"
        }
    }).inserted_id

    # 3. Registrar pago de suscripción completado ($1,300)
    pago_id = db["suscripciones_pagos"].insert_one({
        "tallerTenantId": tenant_id,
        "monto": 1300.00,
        "concepto": "Suscripción Mensual Mekanics Manager",
        "estado": "COMPLETADO",
        "metodo": "CARD",
        "folioClip": "CLIP-12345",
        "fechaPago": datetime.utcnow()
    }).inserted_id

    # 4. Mocks de PAC SW Sapien
    monkeypatch.setenv("SW_URL", "https://mock.swsapiens.com")
    mock_cfdi_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<cfdi:Comprobante xmlns:cfdi="http://www.sat.gob.mx/cfd/4" '
        'Serie="F" Folio="1" Total="1300.00" SubTotal="1120.69">'
        '<cfdi:Emisor Rfc="VABO780711D41" Nombre="OMAR VALDEZ BECERRIL" RegimenFiscal="612"/>'
        '<cfdi:Receptor Rfc="MUMR870225TM1" Nombre="ROBERTO CARLOS MUÑOZ MARQUEZ" RegimenFiscalReceptor="626"/>'
        '</cfdi:Comprobante>'
    )

    with patch("src.handlers.admin.platform_facturacion_manager.get_sw_token", return_value="mock-token"), \
         patch("requests.post") as mock_post:
        
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "status": "success",
            "data": {
                "cfdi": mock_cfdi_xml,
                "uuid": "3E960908-CB82-40E9-9AB1-12DB97213C11",
                "cadenaOriginalSAT": "||cadena-sat||",
                "noCertificadoCFDI": "00001000000714885049",
                "noCertificadoSAT": "00001000000705250068",
                "qrCode": "https://qr.sat.gob.mx",
                "selloCFDI": "sello-cfdi-xyz",
                "selloSAT": "sello-sat-xyz",
                "fechaTimbrado": "2026-09-09T10:15:49"
            }
        }
        mock_post.return_value = mock_response

        event = {
            "body": json.dumps({"pagoId": str(pago_id)}),
            "requestContext": {"authorizer": {"claims": _super_admin_claims()}}
        }

        res = facturar_pago_suscripcion_handler(event, None)
        assert res["statusCode"] == 201, res["body"]

        data = json.loads(res["body"])["data"]
        assert data["uuid"] == "3E960908-CB82-40E9-9AB1-12DB97213C11"
        assert data["folio"] == 1
        assert data["serie"] == "F"
        assert data["total"] == 1300.00
        assert data["subtotal"] == 1120.69
        assert data["rfc_receptor"] == "MUMR870225TM1"

        # Verificar que la factura se persistió en _platform['facturasemitidas']
        factura_guardada = db["facturasemitidas"].find_one({"uuid": "3E960908-CB82-40E9-9AB1-12DB97213C11"})
        assert factura_guardada is not None
        assert factura_guardada["pagoId"] == str(pago_id)

        # Verificar que el pago se actualizó a facturado
        pago_actualizado = db["suscripciones_pagos"].find_one({"_id": pago_id})
        assert pago_actualizado["facturado"] is True
        assert pago_actualizado["uuid"] == "3E960908-CB82-40E9-9AB1-12DB97213C11"

        # Verificar payload enviado a SW Sapien (conceptos y montos correctos)
        called_payload = json.loads(mock_post.call_args[1]["data"])
        assert called_payload["SubTotal"] == 1120.69
        assert called_payload["Total"] == 1300.00
        assert called_payload["Conceptos"][0]["ClaveProdServ"] == FACTURA_CLAVE_PROD_SERV
        assert called_payload["Conceptos"][0]["ClaveUnidad"] == FACTURA_CLAVE_UNIDAD
        assert called_payload["Conceptos"][0]["Descripcion"] == FACTURA_DESCRIPCION

def test_facturar_pago_faltan_datos_fiscales(mock_db):
    """Valida rechazo si el taller no tiene configurados datos fiscales."""
    db = mock_db["_platform"]
    tenant_id = "tenant-sin-datos"
    db["talleres"].insert_one({
        "nombreComercial": "Taller Incompleto",
        "tenantId": tenant_id
    })
    pago_id = db["suscripciones_pagos"].insert_one({
        "tallerTenantId": tenant_id,
        "monto": 599.00,
        "estado": "COMPLETADO"
    }).inserted_id

    event = {
        "body": json.dumps({"pagoId": str(pago_id)}),
        "requestContext": {"authorizer": {"claims": _super_admin_claims()}}
    }
    res = facturar_pago_suscripcion_handler(event, None)
    assert res["statusCode"] == 400
    assert "datos fiscales" in json.loads(res["body"])["message"].lower()

def test_list_platform_facturas(mock_db):
    """Verifica listado paginado de facturas emitidas por la plataforma."""
    db = mock_db["_platform"]
    db["facturasemitidas"].insert_many([
        {"folio": 1, "serie": "F", "uuid": "uuid-1", "total": 1300.0, "createdAt": datetime(2026, 9, 10)},
        {"folio": 2, "serie": "F", "uuid": "uuid-2", "total": 599.0, "createdAt": datetime(2026, 9, 15)}
    ])

    event = {
        "queryStringParameters": {"month": "9", "year": "2026", "page": "1", "limit": "10"},
        "requestContext": {"authorizer": {"claims": _super_admin_claims()}}
    }
    res = list_platform_facturas_handler(event, None)
    assert res["statusCode"] == 200
    data = json.loads(res["body"])["data"]
    assert data["total"] == 2
    assert len(data["items"]) == 2

def test_get_platform_factura_pdf(mock_db):
    """Verifica generación de PDF para factura de plataforma."""
    db = mock_db["_platform"]
    cfdi_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<cfdi:Comprobante xmlns:cfdi="http://www.sat.gob.mx/cfd/4" '
        'Serie="F" Folio="1" Total="1300.00" SubTotal="1120.69">'
        '<cfdi:Emisor Rfc="VABO780711D41" Nombre="MEKANICS MANAGER" RegimenFiscal="612"/>'
        '<cfdi:Receptor Rfc="MUMR870225TM1" Nombre="CLIENTE TALLER" RegimenFiscalReceptor="626"/>'
        '</cfdi:Comprobante>'
    )
    factura_id = db["facturasemitidas"].insert_one({
        "cfdi": cfdi_xml,
        "uuid": "uuid-test-pdf",
        "ticket": "PAGO-123",
        "qrCode": "https://qr.sat.gob.mx",
        "cadenaOriginalSAT": "||cadena||",
        "createdAt": datetime.utcnow()
    }).inserted_id

    event = {
        "pathParameters": {"id": str(factura_id)},
        "requestContext": {"authorizer": {"claims": _super_admin_claims()}}
    }
    res = get_platform_factura_pdf_handler(event, None)
    assert res["statusCode"] == 200
    body = json.loads(res["body"])
    assert "pdf_cfdi_b64" in body["data"]
    assert len(body["data"]["pdf_cfdi_b64"]) > 50


def test_limpiar_razon_social():
    """Verifica que los regímenes societarios se eliminen correctamente para cumplir CFDI 4.0."""
    from src.handlers.admin.platform_facturacion_manager import limpiar_razon_social
    assert limpiar_razon_social("ESCUELA KEMPER URGATE SA DE CV") == "ESCUELA KEMPER URGATE"
    assert limpiar_razon_social("ESCUELA KEMPER URGATE, S.A. DE C.V.") == "ESCUELA KEMPER URGATE"
    assert limpiar_razon_social("ESCUELA KEMPER URGATE S.A. DE C.V.") == "ESCUELA KEMPER URGATE"
    assert limpiar_razon_social("ESCUELA KEMPER URGATE S DE RL DE CV") == "ESCUELA KEMPER URGATE"
    assert limpiar_razon_social("EMPRESA SAS") == "EMPRESA"
    assert limpiar_razon_social("JUAN PEREZ LOPEZ") == "JUAN PEREZ LOPEZ"

