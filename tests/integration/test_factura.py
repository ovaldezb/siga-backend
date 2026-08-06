import json
from bson import ObjectId
from unittest.mock import patch, MagicMock
from src.handlers.facturacion.factura_manager import timbrar_factura_handler

TENANT = "tallertest"

def _claims():
    return {"custom:tenant_id": TENANT, "sub": "user-1", "email": "test@taller.com", "name": "Tester"}

def test_timbrar_factura_success(mock_db, monkeypatch):
    db = mock_db[f"t_{TENANT}"]
    
    # Seed sucursal
    suc_id = db["sucursales"].insert_one({
        "nombre": "Sucursal Norte",
        "serie": "A",
        "regimen_fiscal": "601",
        "codigo_postal": "12345",
        "tenant_id": TENANT
    }).inserted_id
    
    # Payload for the lambda
    event = {
        "body": json.dumps({
            "timbrado": {
                "Version": "4.0",
                "Receptor": {
                    "Rfc": "XAXX010101000",
                    "Nombre": "PUBLICO GENERAL",
                    "DomicilioFiscalReceptor": "12345",
                    "RegimenFiscalReceptor": "616",
                    "UsoCFDI": "S01"
                },
                "Conceptos": [],
                "Impuestos": {"TotalImpuestosTrasladados": 0}
            },
            "sucursal": str(suc_id),
            "ticket": "V-2026-0001",
            "idCertificado": "cert-123",
            "fechaVenta": "2026-08-05T19:00:00",
            "email": "cliente@test.com"
        }),
        "requestContext": {"authorizer": {"claims": _claims()}}
    }

    # Mock get_sw_token and requests.post
    monkeypatch.setenv("SW_URL", "https://mock.swsapiens.com")
    
    with patch("src.handlers.facturacion.factura_manager.get_sw_token", return_value="mock-token"), \
         patch("requests.post") as mock_post:
        
        # Configure Mock Response from SW Sapiens
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "status": "success",
            "data": {
                "cfdi": '<?xml version="1.0" encoding="UTF-8"?><cfdi:Comprobante xmlns:cfdi="http://www.sat.gob.mx/cfd/4"/>',
                "uuid": "uuid-123-456",
                "cadenaOriginalSAT": "||cadena-original||",
                "noCertificadoCFDI": "00001000000500000001",
                "noCertificadoSAT": "00001000000500000002",
                "qrCode": "https://qr.sat.gob.mx",
                "selloCFDI": "sello-cfdi",
                "selloSAT": "sello-sat",
                "fechaTimbrado": "2026-08-05T19:00:00"
            }
        }
        mock_post.return_value = mock_response

        # Execute handler
        response = timbrar_factura_handler(event, None)
        assert response["statusCode"] == 200, response["body"]
        
        data = json.loads(response["body"])["data"]
        assert data["uuid"] == "uuid-123-456"
        assert data["folio"] == 1
        assert data["serie"] == "A"

        # Verify folio is stored in DB
        folio = db["folios"].find_one({"tipo": "factura", "sucursal_id": str(suc_id)})
        assert folio["secuencia"] == 1

        # Verify invoice is saved in DB
        saved_factura = db["facturasemitidas"].find_one({"uuid": "uuid-123-456"})
        assert saved_factura is not None
        assert saved_factura["ticket"] == "V-2026-0001"
