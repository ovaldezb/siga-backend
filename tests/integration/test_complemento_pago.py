import json
from bson import ObjectId
from unittest.mock import patch, MagicMock
from src.handlers.facturacion.factura_manager import (
    get_factura_complementos_handler,
    timbrar_complemento_pago_handler,
    list_facturas_handler
)

TENANT = "tallertest"

def _claims():
    return {"custom:tenant_id": TENANT, "sub": "user-1", "email": "test@taller.com", "name": "Tester"}

MOCK_PPD_CFDI = '''<?xml version="1.0" encoding="UTF-8"?>
<cfdi:Comprobante xmlns:cfdi="http://www.sat.gob.mx/cfd/4" Version="4.0" Serie="F" Folio="101" Fecha="2026-09-17T10:00:00"
    SubTotal="1000.00" Moneda="MXN" Total="1160.00" TipoDeComprobante="I" MetodoPago="PPD" FormaPago="99" LugarExpedicion="06000">
    <cfdi:Emisor Rfc="EKU9003173C9" Nombre="ESCUELA KEMPER URGATE" RegimenFiscal="601"/>
    <cfdi:Receptor Rfc="XAXX010101000" Nombre="PUBLICO GENERAL" DomicilioFiscalReceptor="06000" RegimenFiscalReceptor="616" UsoCFDI="G03"/>
    <cfdi:Conceptos>
        <cfdi:Concepto ClaveProdServ="01010101" Cantidad="1" ClaveUnidad="H87" Descripcion="Servicio automotriz" ValorUnitario="1000.00" Importe="1000.00" ObjetoImp="02">
            <cfdi:Impuestos>
                <cfdi:Traslados>
                    <cfdi:Traslado Base="1000.00" Impuesto="002" TipoFactor="Tasa" TasaOCuota="0.160000" Importe="160.00"/>
                </cfdi:Traslados>
            </cfdi:Impuestos>
        </cfdi:Concepto>
    </cfdi:Conceptos>
    <cfdi:Impuestos TotalImpuestosTrasladados="160.00">
        <cfdi:Traslados>
            <cfdi:Traslado Base="1000.00" Impuesto="002" TipoFactor="Tasa" TasaOCuota="0.160000" Importe="160.00"/>
        </cfdi:Traslados>
    </cfdi:Impuestos>
</cfdi:Comprobante>'''

def _seed_parent_factura(db, metodo_pago="PPD", estatus="Vigente", total=1160.0):
    suc_id = db["sucursales"].insert_one({
        "nombre": "Sucursal Centro",
        "serie": "F",
        "serie_pago": "P",
        "regimen_fiscal": "601",
        "codigo_postal": "06000",
        "tenant_id": TENANT
    }).inserted_id

    factura_id = db["facturasemitidas"].insert_one({
        "uuid": "UUID-PADRE-1234-5678",
        "serie": "F",
        "folio": 101,
        "tipo_de_comprobante": "I",
        "rfc_receptor": "XAXX010101000",
        "nombre_receptor": "PUBLICO GENERAL",
        "domicilio_fiscal_receptor": "06000",
        "regimen_fiscal_receptor": "616",
        "forma_pago": "99",
        "metodo_pago": metodo_pago,
        "subtotal": 1000.0,
        "total": total,
        "saldo_insoluto": total,
        "esta_liquidada": False,
        "sucursal": str(suc_id),
        "idCertificado": "cert-test-01",
        "ticket": "T-1001",
        "cfdi": MOCK_PPD_CFDI,
        "estatus": estatus,
        "tenant_id": TENANT
    }).inserted_id

    return str(factura_id), str(suc_id)

def test_get_factura_complementos_initial(mock_db):
    db = mock_db[f"t_{TENANT}"]
    factura_id, _ = _seed_parent_factura(db)

    event = {
        "pathParameters": {"id": factura_id},
        "requestContext": {"authorizer": {"claims": _claims()}}
    }

    response = get_factura_complementos_handler(event, None)
    assert response["statusCode"] == 200, response["body"]
    
    body = json.loads(response["body"])
    data = body["data"]
    assert data["total_factura"] == 1160.0
    assert data["total_pagado"] == 0.0
    assert data["saldo_insoluto"] == 1160.0
    assert data["esta_liquidada"] is False
    assert data["proxima_parcialidad"] == 1
    assert len(data["complementos"]) == 0

def test_timbrar_complemento_validations(mock_db, monkeypatch):
    db = mock_db[f"t_{TENANT}"]
    factura_id, suc_id = _seed_parent_factura(db)
    monkeypatch.setenv("SW_URL", "https://mock.swsapiens.com")

    # 1. Monto negativo o cero
    event_bad_monto = {
        "pathParameters": {"id": factura_id},
        "body": json.dumps({"monto": 0, "forma_pago": "03"}),
        "requestContext": {"authorizer": {"claims": _claims()}}
    }
    res = timbrar_complemento_pago_handler(event_bad_monto, None)
    assert res["statusCode"] == 400
    assert "mayor a 0" in json.loads(res["body"])["message"]

    # 2. Forma de pago 99 inválida para complemento
    event_bad_forma = {
        "pathParameters": {"id": factura_id},
        "body": json.dumps({"monto": 500, "forma_pago": "99"}),
        "requestContext": {"authorizer": {"claims": _claims()}}
    }
    res = timbrar_complemento_pago_handler(event_bad_forma, None)
    assert res["statusCode"] == 400
    assert "99" in json.loads(res["body"])["message"]

    # 3. Monto excede saldo
    event_excede = {
        "pathParameters": {"id": factura_id},
        "body": json.dumps({"monto": 2000, "forma_pago": "03"}),
        "requestContext": {"authorizer": {"claims": _claims()}}
    }
    res = timbrar_complemento_pago_handler(event_excede, None)
    assert res["statusCode"] == 400
    assert "excede el saldo" in json.loads(res["body"])["message"]

    # 4. Factura padre cancelada
    db["facturasemitidas"].update_one({"_id": ObjectId(factura_id)}, {"$set": {"estatus": "Cancelada"}})
    event_cancelada = {
        "pathParameters": {"id": factura_id},
        "body": json.dumps({"monto": 500, "forma_pago": "03"}),
        "requestContext": {"authorizer": {"claims": _claims()}}
    }
    res = timbrar_complemento_pago_handler(event_cancelada, None)
    assert res["statusCode"] == 400
    assert "cancelada" in json.loads(res["body"])["message"]

def test_timbrar_complementos_flow(mock_db, monkeypatch):
    """Test full cycle: Parcialidad 1 -> Parcialidad 2 (Liquidada) -> Attempt Parcialidad 3 (Rejected)."""
    db = mock_db[f"t_{TENANT}"]
    factura_id, suc_id = _seed_parent_factura(db, total=1160.0)
    monkeypatch.setenv("SW_URL", "https://mock.swsapiens.com")

    mock_rep_xml = '''<?xml version="1.0" encoding="UTF-8"?>
<cfdi:Comprobante xmlns:cfdi="http://www.sat.gob.mx/cfd/4" xmlns:pago20="http://www.sat.gob.mx/Pagos20"
    Version="4.0" Serie="P" Folio="1" Fecha="2026-09-17T12:00:00" SubTotal="0" Moneda="XXX" Total="0" TipoDeComprobante="P" Exportacion="01" LugarExpedicion="06000">
    <cfdi:Emisor Rfc="EKU9003173C9" Nombre="ESCUELA KEMPER URGATE" RegimenFiscal="601"/>
    <cfdi:Receptor Rfc="XAXX010101000" Nombre="PUBLICO GENERAL" DomicilioFiscalReceptor="06000" RegimenFiscalReceptor="616" UsoCFDI="CP01"/>
    <cfdi:Conceptos>
        <cfdi:Concepto ClaveProdServ="84111506" Cantidad="1" ClaveUnidad="ACT" Descripcion="Pago" ValorUnitario="0" Importe="0" ObjetoImp="01"/>
    </cfdi:Conceptos>
    <cfdi:Complemento>
        <pago20:Pagos Version="2.0">
            <pago20:Totales TotalTrasladosBaseIVA16="431.03" TotalTrasladosImpuestoIVA16="68.97" MontoTotalPagos="500.00"/>
            <pago20:Pago FechaPago="2026-09-17T12:00:00" FormaDePagoP="03" MonedaP="MXN" TipoCambioP="1" Monto="500.00">
                <pago20:DoctoRelacionado IdDocumento="UUID-PADRE-1234-5678" Serie="F" Folio="101" MonedaDR="MXN" EquivalenciaDR="1" NumParcialidad="1" ImpSaldoAnt="1160.00" ImpPagado="500.00" ImpSaldoInsoluto="660.00" ObjetoImpDR="02"/>
            </pago20:Pago>
        </pago20:Pagos>
    </cfdi:Complemento>
</cfdi:Comprobante>'''

    with patch("src.handlers.facturacion.factura_manager.get_sw_token", return_value="mock-token"), \
         patch("requests.post") as mock_post:

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": "success",
            "data": {
                "cfdi": mock_rep_xml,
                "uuid": "UUID-COMPLEMENTO-P1",
                "cadenaOriginalSAT": "||cadena-rep-1||",
                "noCertificadoCFDI": "00001000000500000001",
                "noCertificadoSAT": "00001000000500000002",
                "qrCode": "https://qr.sat.gob.mx/rep1",
                "selloCFDI": "sello-cfdi-rep",
                "selloSAT": "sello-sat-rep",
                "fechaTimbrado": "2026-09-17T12:00:00"
            }
        }
        mock_post.return_value = mock_resp

        # --- PARCIALIDAD 1: $500.00 ---
        event_p1 = {
            "pathParameters": {"id": factura_id},
            "body": json.dumps({
                "monto": 500.0,
                "forma_pago": "03",
                "fecha_pago": "2026-09-17T12:00:00",
                "num_operacion": "SPEI-001"
            }),
            "requestContext": {"authorizer": {"claims": _claims()}}
        }

        res1 = timbrar_complemento_pago_handler(event_p1, None)
        assert res1["statusCode"] == 200, res1["body"]
        data1 = json.loads(res1["body"])["data"]
        assert data1["num_parcialidad"] == 1
        assert data1["imp_saldo_ant"] == 1160.0
        assert data1["imp_pagado"] == 500.0
        assert data1["imp_saldo_insoluto"] == 660.0
        assert data1["esta_liquidada"] is False
        assert data1["uuid"] == "UUID-COMPLEMENTO-P1"
        assert data1.get("pdf_cfdi_b64") is not None

        # Verify parent invoice balance updated in DB
        factura_db = db["facturasemitidas"].find_one({"_id": ObjectId(factura_id)})
        assert factura_db["saldo_insoluto"] == 660.0
        assert factura_db["esta_liquidada"] is False
        assert factura_db["ultima_parcialidad"] == 1

        # Check get_factura_complementos reflects Parcialidad 1
        history_event = {
            "pathParameters": {"id": factura_id},
            "requestContext": {"authorizer": {"claims": _claims()}}
        }
        res_hist = get_factura_complementos_handler(history_event, None)
        data_hist = json.loads(res_hist["body"])["data"]
        assert data_hist["total_pagado"] == 500.0
        assert data_hist["saldo_insoluto"] == 660.0
        assert data_hist["proxima_parcialidad"] == 2
        assert len(data_hist["complementos"]) == 1

        # --- PARCIALIDAD 2: $660.00 (Liquidación total) ---
        mock_resp.json.return_value["data"]["uuid"] = "UUID-COMPLEMENTO-P2"
        event_p2 = {
            "pathParameters": {"id": factura_id},
            "body": json.dumps({
                "monto": 660.0,
                "forma_pago": "03",
                "fecha_pago": "2026-09-17T15:30:00",
                "num_operacion": "SPEI-002"
            }),
            "requestContext": {"authorizer": {"claims": _claims()}}
        }

        res2 = timbrar_complemento_pago_handler(event_p2, None)
        assert res2["statusCode"] == 200, res2["body"]
        data2 = json.loads(res2["body"])["data"]
        assert data2["num_parcialidad"] == 2
        assert data2["imp_saldo_ant"] == 660.0
        assert data2["imp_pagado"] == 660.0
        assert data2["imp_saldo_insoluto"] == 0.0
        assert data2["esta_liquidada"] is True

        # Verify parent invoice is now liquidada
        factura_db2 = db["facturasemitidas"].find_one({"_id": ObjectId(factura_id)})
        assert factura_db2["saldo_insoluto"] == 0.0
        assert factura_db2["esta_liquidada"] is True

        # --- ATTEMPT PARCIALIDAD 3 (Rejected because already liquidada) ---
        event_p3 = {
            "pathParameters": {"id": factura_id},
            "body": json.dumps({
                "monto": 100.0,
                "forma_pago": "03"
            }),
            "requestContext": {"authorizer": {"claims": _claims()}}
        }
        res3 = timbrar_complemento_pago_handler(event_p3, None)
        assert res3["statusCode"] == 400
        assert "totalmente liquidada" in json.loads(res3["body"])["message"]
