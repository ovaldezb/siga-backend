import json
from bson import ObjectId
from unittest.mock import patch, MagicMock
from src.handlers.facturacion.factura_manager import (
    cancelar_factura_handler
)

TENANT = "tallertest"

def _claims():
    return {"custom:tenant_id": TENANT, "sub": "user-1", "email": "test@taller.com", "name": "Tester"}

MOCK_XML = '''<?xml version="1.0" encoding="UTF-8"?>
<cfdi:Comprobante xmlns:cfdi="http://www.sat.gob.mx/cfd/4" Version="4.0" Serie="F" Folio="101" Fecha="2026-09-17T10:00:00"
    SubTotal="1000.00" Moneda="MXN" Total="1160.00" TipoDeComprobante="I" MetodoPago="PUE" FormaPago="03" LugarExpedicion="06000">
    <cfdi:Emisor Rfc="EKU9003173C9" Nombre="ESCUELA KEMPER URGATE" RegimenFiscal="601"/>
    <cfdi:Receptor Rfc="XAXX010101000" Nombre="PUBLICO GENERAL" DomicilioFiscalReceptor="06000" RegimenFiscalReceptor="616" UsoCFDI="G03"/>
</cfdi:Comprobante>'''

def _seed_factura(db, estatus="Vigente", uuid="11111111-2222-3333-4444-555555555555", tipo="I", ticket="TK-001"):
    suc_id = db["sucursales"].insert_one({
        "nombre": "Sucursal Matriz",
        "rfc": "EKU9003173C9",
        "tenant_id": TENANT
    }).inserted_id

    if ticket:
        db["ventas"].insert_one({
            "folio": ticket,
            "venta_facturada": True,
            "tenant_id": TENANT
        })

    factura_id = db["facturasemitidas"].insert_one({
        "uuid": uuid,
        "serie": "F",
        "folio": 101,
        "tipo_de_comprobante": tipo,
        "rfc_receptor": "XAXX010101000",
        "nombre_receptor": "PUBLICO GENERAL",
        "ticket": ticket,
        "total": 1160.0,
        "saldo_insoluto": 1160.0 if tipo != "P" else 0.0,
        "esta_liquidada": False if tipo != "P" else True,
        "estatus": estatus,
        "sucursal": str(suc_id),
        "cfdi": MOCK_XML,
        "tenant_id": TENANT
    }).inserted_id

    return str(factura_id), uuid

def _mock_sw_success(uuid="11111111-2222-3333-4444-555555555555", estatus_uuid="201"):
    mock_res = MagicMock()
    mock_res.status_code = 200
    mock_res.json.return_value = {
        "status": "success",
        "data": {
            "acuse": "<Acuse xmlns=\"http://cancelacfd.sat.gob.mx\"><Folios><UUID>11111111-2222-3333-4444-555555555555</UUID></Folios></Acuse>",
            "folios": [
                {
                    "uuid": uuid,
                    "estatusUUID": estatus_uuid,
                    "respuesta": "Solicitud aceptada"
                }
            ]
        }
    }
    return mock_res

def test_cancelar_factura_motivo_invalido(mock_db):
    db = mock_db[f"t_{TENANT}"]
    f_id, _ = _seed_factura(db)
    event = {
        "pathParameters": {"id": f_id},
        "requestContext": {"authorizer": {"claims": _claims()}},
        "body": json.dumps({"motivo": "99"})
    }
    res = cancelar_factura_handler(event, None)
    assert res["statusCode"] == 400
    assert "Motivo de cancelación inválido" in json.loads(res["body"])["message"]

def test_cancelar_factura_ya_cancelada(mock_db):
    db = mock_db[f"t_{TENANT}"]
    f_id, _ = _seed_factura(db, estatus="Cancelada")
    event = {
        "pathParameters": {"id": f_id},
        "requestContext": {"authorizer": {"claims": _claims()}},
        "body": json.dumps({"motivo": "02"})
    }
    res = cancelar_factura_handler(event, None)
    assert res["statusCode"] == 400
    assert "ya se encuentra cancelada" in json.loads(res["body"])["message"]

def test_cancelar_factura_motivo_01_sin_sustituto(mock_db):
    db = mock_db[f"t_{TENANT}"]
    f_id, _ = _seed_factura(db)
    event = {
        "pathParameters": {"id": f_id},
        "requestContext": {"authorizer": {"claims": _claims()}},
        "body": json.dumps({"motivo": "01", "folio_sustitucion": ""})
    }
    res = cancelar_factura_handler(event, None)
    assert res["statusCode"] == 400
    assert "requiere especificar el UUID" in json.loads(res["body"])["message"]

def test_cancelar_factura_motivo_01_mismo_uuid(mock_db):
    db = mock_db[f"t_{TENANT}"]
    f_id, uuid = _seed_factura(db)
    event = {
        "pathParameters": {"id": f_id},
        "requestContext": {"authorizer": {"claims": _claims()}},
        "body": json.dumps({"motivo": "01", "folio_sustitucion": uuid})
    }
    res = cancelar_factura_handler(event, None)
    assert res["statusCode"] == 400
    assert "no puede ser igual" in json.loads(res["body"])["message"]

def test_cancelar_factura_con_complementos_vigentes(mock_db):
    db = mock_db[f"t_{TENANT}"]
    f_id, uuid = _seed_factura(db)
    db["facturasemitidas"].insert_one({
        "factura_padre_id": f_id,
        "factura_padre_uuid": uuid,
        "tipo_de_comprobante": "P",
        "imp_pagado": 500.0,
        "estatus": "Vigente",
        "tenant_id": TENANT
    })

    event = {
        "pathParameters": {"id": f_id},
        "requestContext": {"authorizer": {"claims": _claims()}},
        "body": json.dumps({"motivo": "02"})
    }
    res = cancelar_factura_handler(event, None)
    assert res["statusCode"] == 400
    assert "complemento(s) de pago vigentes" in json.loads(res["body"])["message"]

@patch("src.handlers.facturacion.factura_manager.get_sw_token", return_value="mock-sw-token")
@patch("requests.post")
def test_cancelar_factura_exitosa_motivo_02(mock_post, mock_tok, mock_db):
    db = mock_db[f"t_{TENANT}"]
    f_id, uuid = _seed_factura(db, ticket="TK-001")
    mock_post.return_value = _mock_sw_success(uuid=uuid, estatus_uuid="201")

    event = {
        "pathParameters": {"id": f_id},
        "requestContext": {"authorizer": {"claims": _claims()}},
        "body": json.dumps({"motivo": "02"})
    }
    res = cancelar_factura_handler(event, None)
    assert res["statusCode"] == 200
    body = json.loads(res["body"])
    assert body["data"]["estatus"] == "Cancelada"
    assert body["data"]["estatusSAT"] == "201"

    doc = db["facturasemitidas"].find_one({"_id": ObjectId(f_id)})
    assert doc["estatus"] == "Cancelada"
    assert doc["motivo_cancelacion"] == "02"
    assert "acuse_cancelacion" in doc

    venta = db["ventas"].find_one({"folio": "TK-001"})
    assert venta["venta_facturada"] is False

    args, kwargs = mock_post.call_args
    assert f"/cfdi33/cancel/EKU9003173C9/{uuid}/02" in args[0]

@patch("src.handlers.facturacion.factura_manager.get_sw_token", return_value="mock-sw-token")
@patch("requests.post")
def test_cancelar_factura_exitosa_motivo_01(mock_post, mock_tok, mock_db):
    db = mock_db[f"t_{TENANT}"]
    f_id, uuid = _seed_factura(db)
    sustituto = "99999999-8888-7777-6666-555555555555"
    mock_post.return_value = _mock_sw_success(uuid=uuid, estatus_uuid="201")

    event = {
        "pathParameters": {"id": f_id},
        "requestContext": {"authorizer": {"claims": _claims()}},
        "body": json.dumps({"motivo": "01", "folio_sustitucion": sustituto})
    }
    res = cancelar_factura_handler(event, None)
    assert res["statusCode"] == 200
    body = json.loads(res["body"])
    assert body["data"]["estatus"] == "Cancelada"
    assert body["data"]["folio_sustitucion"] == sustituto

    args, kwargs = mock_post.call_args
    assert f"/cfdi33/cancel/EKU9003173C9/{uuid}/01/{sustituto}" in args[0]

@patch("src.handlers.facturacion.factura_manager.get_sw_token", return_value="mock-sw-token")
@patch("requests.post")
def test_cancelar_complemento_pago_recalcula_saldo_padre(mock_post, mock_tok, mock_db):
    db = mock_db[f"t_{TENANT}"]
    padre_id, padre_uuid = _seed_factura(db)
    
    comp1_id = db["facturasemitidas"].insert_one({
        "uuid": "COMP-1111-2222-3333-444444444444",
        "tipo_de_comprobante": "P",
        "factura_padre_id": padre_id,
        "factura_padre_uuid": padre_uuid,
        "imp_pagado": 500.0,
        "estatus": "Vigente",
        "cfdi": MOCK_XML,
        "tenant_id": TENANT
    }).inserted_id

    comp2_id = db["facturasemitidas"].insert_one({
        "uuid": "COMP-5555-6666-7777-888888888888",
        "tipo_de_comprobante": "P",
        "factura_padre_id": padre_id,
        "factura_padre_uuid": padre_uuid,
        "imp_pagado": 500.0,
        "estatus": "Vigente",
        "cfdi": MOCK_XML,
        "tenant_id": TENANT
    }).inserted_id

    db["facturasemitidas"].update_one(
        {"_id": ObjectId(padre_id)},
        {"$set": {"saldo_insoluto": 160.0, "esta_liquidada": False}}
    )

    mock_post.return_value = _mock_sw_success(uuid="COMP-5555-6666-7777-888888888888", estatus_uuid="201")
    event = {
        "pathParameters": {"id": str(comp2_id)},
        "requestContext": {"authorizer": {"claims": _claims()}},
        "body": json.dumps({"motivo": "02"})
    }
    res = cancelar_factura_handler(event, None)
    assert res["statusCode"] == 200

    c2 = db["facturasemitidas"].find_one({"_id": comp2_id})
    assert c2["estatus"] == "Cancelada"

    padre = db["facturasemitidas"].find_one({"_id": ObjectId(padre_id)})
    assert padre["saldo_insoluto"] == 660.0
    assert padre["esta_liquidada"] is False

@patch("src.handlers.facturacion.factura_manager.get_sw_token", return_value="mock-sw-token")
@patch("requests.post")
def test_cancelar_factura_sat_rechazo_203(mock_post, mock_tok, mock_db):
    db = mock_db[f"t_{TENANT}"]
    f_id, uuid = _seed_factura(db)
    mock_res = MagicMock()
    mock_res.status_code = 200
    mock_res.json.return_value = {
        "status": "success",
        "data": {
            "acuse": "",
            "folios": [
                {
                    "uuid": uuid,
                    "estatusUUID": "203",
                    "respuesta": "No cancelable, tiene documentos relacionados vigentes"
                }
            ]
        }
    }
    mock_post.return_value = mock_res

    event = {
        "pathParameters": {"id": f_id},
        "requestContext": {"authorizer": {"claims": _claims()}},
        "body": json.dumps({"motivo": "02"})
    }
    res = cancelar_factura_handler(event, None)
    assert res["statusCode"] == 400
    assert "203" in json.loads(res["body"])["message"]
