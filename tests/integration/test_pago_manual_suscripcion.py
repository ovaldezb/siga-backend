"""Pago manual de suscripción (POST /admin/pagos/manual) y su factura.

Un taller que paga por transferencia directa, terminal o efectivo no genera webhook
de Openpay. Lo que se fija aquí:
- Solo el SUPER_ADMIN lo registra, y queda como un pago COMPLETADO equivalente al automático.
- La clave SAT sale de la forma de pago; en TPV, de si la tarjeta fue débito o crédito.
- Extiende la suscripción y reactiva el taller, salvo que se pida no hacerlo.
- No se puede capturar dos veces el mismo comprobante.
- La factura manual usa esa clave SAT, exige "Requiere factura" y envía el correo.
- El historial de pagos de otro taller solo lo ve el SUPER_ADMIN.
"""
import json
from datetime import datetime
from unittest.mock import MagicMock, patch

from src.handlers.admin.pagos_manager import obtener_historial_pagos_handler, registrar_pago_manual_handler
from src.handlers.admin.platform_facturacion_manager import facturar_pago_suscripcion_handler

TENANT = "tenant-pago-manual"
SUPER = {"cognito:groups": ["SUPER_ADMIN"], "email": "super@mekanics.mx"}


def _evento(body=None, claims=None, qs=None):
    return {
        "body": json.dumps(body or {}),
        "queryStringParameters": qs or {},
        "requestContext": {"authorizer": {"claims": SUPER if claims is None else claims}},
    }


def _taller(db, **extra):
    doc = {
        "tenantId": TENANT, "nombreComercial": "Taller Manual", "adminEmail": "dueno@taller.mx",
        "estado": "INACTIVO", "mesesCargo": 1, "precioSuscripcion": 599.0,
        "proximaFechaCorte": datetime(2026, 9, 20), "proximaFechaPago": datetime(2026, 9, 30),
        **extra,
    }
    db["talleres"].insert_one(doc)
    return doc


def _registrar(**cambios):
    body = {"tallerTenantId": TENANT, "formaPago": "TRANSFERENCIA", "monto": 599, "fechaPago": "2026-10-10",
            "referencia": "RASTREO-001", **cambios}
    resp = registrar_pago_manual_handler(_evento(body), None)
    return resp["statusCode"], json.loads(resp["body"])


def test_transferencia_queda_como_pago_completado_y_reactiva(mock_db):
    db = mock_db["_platform"]
    _taller(db)
    status, r = _registrar(banco="BBVA", notas="Transferencia directa a la cuenta")
    assert status == 201, r
    d = r["data"]
    assert (d["estado"], d["origen"], d["forma_pago_sat"], d["metodo"]) == (
        "COMPLETADO", "MANUAL", "03", "Transferencia directa (BBVA)")
    assert d["suscripcionExtendida"] is True and d["facturado"] is False

    pago = db["suscripciones_pagos"].find_one({"tallerTenantId": TENANT})
    assert (pago["monto"], pago["estado"], pago["folioClip"], pago["referencia"]) == (599.0, "COMPLETADO", "RASTREO-001", "RASTREO-001")
    assert (pago["usuarioEmail"], pago["registradoPor"], pago["banco"]) == ("dueno@taller.mx", "super@mekanics.mx", "BBVA")
    assert pago["fechaPago"] == datetime(2026, 10, 10, 12) and pago["concepto"] == "Suscripción Mensual Mekanics Manager"

    # Pagó después de su fecha límite: mismo cálculo que el webhook, y vuelve a ACTIVO.
    taller = db["talleres"].find_one({"tenantId": TENANT})
    assert taller["estado"] == "ACTIVO"
    assert taller["proximaFechaCorte"] == datetime(2026, 10, 31, 12)
    assert taller["proximaFechaPago"] == datetime(2026, 11, 10, 12)


def test_tpv_pide_debito_o_credito(mock_db):
    db = mock_db["_platform"]
    _taller(db)
    assert _registrar(formaPago="TPV", referencia="AUT-1")[0] == 400
    assert _registrar(formaPago="TPV", referencia="AUT-1", tipoTarjeta="PREPAGO")[0] == 400
    assert _registrar(formaPago="TPV", referencia="", tipoTarjeta="DEBITO")[0] == 400

    status, r = _registrar(formaPago="TPV", referencia="AUT-1", tipoTarjeta="debito", marca="visa", ultimos4="xx4321")
    assert status == 201 and (r["data"]["forma_pago_sat"], r["data"]["metodo"]) == ("28", "Tarjeta de Débito (TPV - VISA •••• 4321)")
    status, r = _registrar(formaPago="TPV", referencia="AUT-2", tipoTarjeta="CREDITO")
    assert status == 201 and (r["data"]["forma_pago_sat"], r["data"]["metodo"]) == ("04", "Tarjeta de Crédito (TPV)")

    pago = db["suscripciones_pagos"].find_one({"referencia": "AUT-1"})
    assert (pago["tipoTarjeta"], pago["marcaTarjeta"], pago["ultimos4"]) == ("DEBITO", "VISA", "4321")


def test_efectivo_sin_referencia_y_sin_extender(mock_db):
    db = mock_db["_platform"]
    _taller(db)
    status, r = _registrar(formaPago="EFECTIVO", referencia="", extenderSuscripcion=False)
    assert status == 201
    assert (r["data"]["forma_pago_sat"], r["data"]["metodo"], r["data"]["suscripcionExtendida"]) == ("01", "Efectivo", False)
    assert r["data"]["tokenClip"].startswith("MANUAL-")

    taller = db["talleres"].find_one({"tenantId": TENANT})
    assert taller["estado"] == "INACTIVO" and taller["proximaFechaCorte"] == datetime(2026, 9, 20)


def test_validaciones_y_duplicado(mock_db):
    db = mock_db["_platform"]
    _taller(db)
    assert registrar_pago_manual_handler(_evento({"tallerTenantId": TENANT}, claims={"cognito:groups": ["ADMIN"]}), None)["statusCode"] == 403
    assert _registrar(formaPago="CHEQUE")[0] == 400
    assert _registrar(monto=0)[0] == 400
    assert _registrar(monto="abc")[0] == 400
    assert _registrar(fechaPago="10/10/2026")[0] == 400
    assert _registrar(fechaPago="2099-01-01")[0] == 400
    assert _registrar(referencia="")[0] == 400            # transferencia sin clave de rastreo
    assert _registrar(tallerTenantId="no-existe")[0] == 404
    assert db["suscripciones_pagos"].count_documents({}) == 0

    assert _registrar()[0] == 201
    status, r = _registrar()                                # mismo comprobante otra vez
    assert status == 409 and "RASTREO-001" in r["message"]
    # También choca con un pago que Openpay sí registró con esa referencia.
    db["suscripciones_pagos"].insert_one({"tallerTenantId": TENANT, "folioClip": "trx-openpay"})
    assert _registrar(referencia="trx-openpay")[0] == 409
    assert db["suscripciones_pagos"].count_documents({"origen": "MANUAL"}) == 1


def _preparar_facturacion(db, **taller_extra):
    db["sucursales"].insert_one({"is_platform": True, "rfc": "VABO780711D41", "nombre": "OMAR VALDEZ BECERRIL",
                                 "regimen_fiscal": "612", "codigo_postal": "50000", "serie": "F"})
    db["certificates"].insert_one({"no_certificado": "00001000000714885049"})
    _taller(db, datosFiscales={"rfc": "MUMR870225TM1", "razonSocial": "ROBERTO CARLOS MUÑOZ MARQUEZ",
                               "codigoPostal": "20030", "regimenFiscal": "626", "usoCfdi": "G03"}, **taller_extra)


def _respuesta_pac():
    xml = ('<?xml version="1.0" encoding="UTF-8"?><cfdi:Comprobante xmlns:cfdi="http://www.sat.gob.mx/cfd/4" '
           'Serie="F" Folio="1" Total="599.00" SubTotal="516.38">'
           '<cfdi:Emisor Rfc="VABO780711D41" Nombre="OMAR VALDEZ BECERRIL" RegimenFiscal="612"/>'
           '<cfdi:Receptor Rfc="MUMR870225TM1" Nombre="ROBERTO CARLOS MUÑOZ MARQUEZ" RegimenFiscalReceptor="626"/>'
           '</cfdi:Comprobante>')
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"status": "success", "data": {
        "cfdi": xml, "uuid": "AAAA1111-CB82-40E9-9AB1-12DB97213C11", "cadenaOriginalSAT": "||c||",
        "qrCode": "https://qr.sat.gob.mx", "fechaTimbrado": "2026-10-10T10:15:49"}}
    return resp


def test_factura_del_pago_manual_usa_su_clave_sat_y_envia_correo(mock_db, monkeypatch):
    db = mock_db["_platform"]
    _preparar_facturacion(db, requiereFactura=True)
    monkeypatch.setenv("SW_URL", "https://mock.swsapiens.com")
    pago_id = _registrar(formaPago="EFECTIVO", referencia="")[1]["data"]["id"]

    modulo = "src.handlers.admin.platform_facturacion_manager"
    with patch(f"{modulo}.get_sw_token", return_value="tok"), patch("requests.post", return_value=_respuesta_pac()) as post, \
         patch(f"{modulo}.CFDIPDF_FPDF_Generator") as pdf, patch(f"{modulo}.send_email") as correo:
        pdf.return_value.generate_pdf.return_value = b"%PDF-falso"
        resp = facturar_pago_suscripcion_handler(_evento({"pagoId": pago_id}), None)

    assert resp["statusCode"] == 201, resp["body"]
    data = json.loads(resp["body"])["data"]
    # Efectivo es "01": antes caía en el valor por defecto de tarjeta de crédito.
    assert json.loads(post.call_args[1]["data"])["FormaPago"] == "01"
    assert (data["forma_pago"], data["correo_enviado"], data["correo_destino"]) == ("01", True, "dueno@taller.mx")
    # El generador de PDF recibe la fecha del pago como texto, no como datetime.
    assert pdf.call_args[1]["fecha_hora_venta"] == "2026-10-10T12:00:00"

    enviado = correo.call_args[1]
    assert enviado["to"] == "dueno@taller.mx"
    assert [a["filename"] for a in enviado["attachments"]] == ["Factura-AAAA1111.pdf", "Factura-AAAA1111.xml"]
    assert db["suscripciones_pagos"].find_one({"origen": "MANUAL"})["facturado"] is True
    assert db["facturasemitidas"].find_one({"pagoId": pago_id})["origen"] == "manual_suscripcion"


def test_factura_emitida_aunque_falle_el_correo(mock_db, monkeypatch):
    db = mock_db["_platform"]
    _preparar_facturacion(db, requiereFactura=True)
    monkeypatch.setenv("SW_URL", "https://mock.swsapiens.com")
    pago_id = _registrar()[1]["data"]["id"]

    modulo = "src.handlers.admin.platform_facturacion_manager"
    with patch(f"{modulo}.get_sw_token", return_value="tok"), patch("requests.post", return_value=_respuesta_pac()), \
         patch(f"{modulo}.CFDIPDF_FPDF_Generator") as pdf, patch(f"{modulo}.send_email", side_effect=RuntimeError("smtp caído")):
        pdf.return_value.generate_pdf.return_value = b"%PDF-falso"
        resp = facturar_pago_suscripcion_handler(_evento({"pagoId": pago_id}), None)

    assert resp["statusCode"] == 201
    assert json.loads(resp["body"])["data"]["correo_enviado"] is False
    assert db["suscripciones_pagos"].find_one({"origen": "MANUAL"})["facturado"] is True


def test_no_se_factura_si_el_taller_no_requiere_factura(mock_db):
    db = mock_db["_platform"]
    _preparar_facturacion(db)                                # requiereFactura sin marcar
    status, r = _registrar()
    assert status == 201 and r["data"]["requiereFactura"] is False

    with patch("requests.post") as post:
        resp = facturar_pago_suscripcion_handler(_evento({"pagoId": r["data"]["id"]}), None)
    assert resp["statusCode"] == 400 and "requiere factura" in json.loads(resp["body"])["message"].lower()
    post.assert_not_called()
    assert not db["suscripciones_pagos"].find_one({"origen": "MANUAL"}).get("facturado")


def test_historial_de_otro_taller_solo_para_super_admin(mock_db):
    db = mock_db["_platform"]
    _taller(db)
    _registrar(notas="nota interna")
    db["suscripciones_pagos"].insert_one({"tallerTenantId": "otro-taller", "monto": 100, "estado": "COMPLETADO",
                                          "fechaPago": datetime(2026, 10, 1), "folioClip": "trx-1"})

    def historial(claims, tenant):
        resp = obtener_historial_pagos_handler(_evento(claims=claims, qs={"tallerTenantId": tenant}), None)
        return json.loads(resp["body"])["data"]["items"]

    items = historial(SUPER, TENANT)
    assert len(items) == 1 and (items[0]["origen"], items[0]["referencia"], items[0]["notas"]) == ("MANUAL", "RASTREO-001", "nota interna")

    # Un ADMIN que manda el tenant de otro taller recibe solo lo suyo.
    ajeno = historial({"custom:tenant_id": "otro-taller", "cognito:groups": ["ADMIN"]}, TENANT)
    assert len(ajeno) == 1 and (ajeno[0]["tokenClip"], ajeno[0]["origen"]) == ("trx-1", "AUTOMATICO")
