import json
import base64
import os
import re
import tempfile
import requests
import xml.dom.minidom
import xml.etree.ElementTree as ET
from datetime import datetime
from zoneinfo import ZoneInfo
from bson import ObjectId
from pymongo import ReturnDocument
from requests_toolbelt.multipart import decoder
from cryptography import x509
from cryptography.hazmat.backends import default_backend

from aws_lambda_powertools import Logger
from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.infrastructure.database import get_platform_db
from src.shared.utils.auth_utils import get_claims, is_super_admin
from src.shared.utils.date_utils import iso_utc
from src.handlers.facturacion.certificates_manager import get_sw_token
from src.handlers.facturacion.cfdi_pdf_fpdf_generator import CFDIPDF_FPDF_Generator

logger = Logger()

SW_URL = os.getenv("SW_URL")

# Constantes SAT para Facturación de MekanicsManager
FACTURA_CLAVE_PROD_SERV = "81112100"
FACTURA_CLAVE_UNIDAD = "MON"
FACTURA_UNIDAD = "Mes"
FACTURA_DESCRIPCION = "Servicio de hospedaje de aplicación MekanicsManager"
FACTURA_OBJETO_IMP = "02"


# ==============================================================================
# 1. CSD (Certificados de Sello Digital) para la Plataforma
# ==============================================================================

def create_platform_certificate_handler(event, context):
    """POST /admin/facturacion/certificados
    Sube y registra el CSD de la empresa ante SW Sapien y lo almacena en _platform['certificates'].
    """
    try:
        claims = get_claims(event)
        if not is_super_admin(claims):
            return create_response(403, "Solo un SUPER_ADMIN puede gestionar certificados de plataforma.")

        db = get_platform_db()

        if event.get("isBase64Encoded"):
            body = base64.b64decode(event["body"])
        else:
            body = event["body"].encode()

        content_type = event["headers"].get("Content-Type") or event["headers"].get("content-type")
        multipart_data = decoder.MultipartDecoder(body, content_type)
        key_bytes = None
        cer_bytes = None
        ctrsn = None

        for part in multipart_data.parts:
            content_disposition = part.headers.get(b"Content-Disposition", b"").decode()
            if 'name="key"' in content_disposition:
                key_bytes = part.content
            elif 'name="cer"' in content_disposition:
                cer_bytes = part.content
            elif 'name="ctrsn"' in content_disposition:
                ctrsn = part.text

        if not key_bytes or not cer_bytes or not ctrsn:
            return create_response(400, "Faltan parámetros obligatorios (cer, key, ctrsn).")

        cert = x509.load_der_x509_certificate(cer_bytes, default_backend())
        serial_number = cert.serial_number
        serial_bytes = serial_number.to_bytes((serial_number.bit_length() + 7) // 8, byteorder='big')
        serial_str = serial_bytes.decode('latin1')

        subject = cert.subject.rfc4514_string()
        rfc_match = re.search(r'2\.5\.4\.45=([A-Z0-9]+)', subject)
        rfc = rfc_match.group(1) if rfc_match else None

        nombre_match = re.search(r'CN=([^,]+)', subject)
        nombre = nombre_match.group(1) if nombre_match else None

        not_before = cert.not_valid_before
        not_after = cert.not_valid_after

        b64_key = base64.b64encode(key_bytes).decode("utf-8")
        b64_cer = base64.b64encode(cer_bytes).decode("utf-8")

        cert_body = {
            "type": "stamp",
            "b64Cer": b64_cer,
            "b64Key": b64_key,
            "password": ctrsn
        }

        token = get_sw_token()

        response = requests.post(
            f"{SW_URL}/certificates/save",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}"
            },
            data=json.dumps(cert_body)
        ).json()

        if response.get("messageDetail"):
            return create_response(400, response.get("messageDetail"))

        certificado_doc = {
            "nombre": nombre,
            "rfc": rfc,
            "no_certificado": serial_str,
            "desde": not_before,
            "hasta": not_after,
            "is_platform": True,
            "createdAt": datetime.utcnow()
        }

        result = db["certificates"].insert_one(certificado_doc)
        certificado_doc["_id"] = str(result.inserted_id)
        certificado_doc["desde"] = iso_utc(certificado_doc["desde"])
        certificado_doc["hasta"] = iso_utc(certificado_doc["hasta"])
        certificado_doc["createdAt"] = iso_utc(certificado_doc["createdAt"])

        return create_response(201, "Certificado de plataforma guardado exitosamente", certificado_doc)

    except Exception as e:
        logger.exception("Error al crear certificado de plataforma")
        return handle_exception(e)


def get_platform_certificates_handler(event, context):
    """GET /admin/facturacion/certificados
    Lista los certificados CSD almacenados en _platform['certificates'].
    """
    try:
        claims = get_claims(event)
        if not is_super_admin(claims):
            return create_response(403, "Solo un SUPER_ADMIN puede consultar certificados de plataforma.")

        db = get_platform_db()
        certificates = list(db["certificates"].find())

        for cert in certificates:
            cert["_id"] = str(cert["_id"])
            if "desde" in cert and isinstance(cert["desde"], datetime):
                cert["desde"] = iso_utc(cert["desde"])
            if "hasta" in cert and isinstance(cert["hasta"], datetime):
                cert["hasta"] = iso_utc(cert["hasta"])
            if "createdAt" in cert and isinstance(cert["createdAt"], datetime):
                cert["createdAt"] = iso_utc(cert["createdAt"])

        return create_response(200, "Certificados de plataforma listados", certificates)
    except Exception as e:
        logger.exception("Error al listar certificados de plataforma")
        return handle_exception(e)


def update_platform_certificate_handler(event, context):
    """PUT /admin/facturacion/certificados/{id}
    Renueva un CSD de la plataforma en SW Sapien y _platform['certificates'].
    """
    try:
        claims = get_claims(event)
        if not is_super_admin(claims):
            return create_response(403, "Solo un SUPER_ADMIN puede actualizar certificados de plataforma.")

        db = get_platform_db()
        cert_id = event.get('pathParameters', {}).get('id')
        if not cert_id:
            return create_response(400, "ID de certificado no proporcionado.")

        if event.get("isBase64Encoded"):
            body = base64.b64decode(event["body"])
        else:
            body = event["body"].encode()

        content_type = event["headers"].get("Content-Type") or event["headers"].get("content-type")
        multipart_data = decoder.MultipartDecoder(body, content_type)
        key_bytes = None
        cer_bytes = None
        ctrsn = None

        for part in multipart_data.parts:
            content_disposition = part.headers.get(b"Content-Disposition", b"").decode()
            if 'name="key"' in content_disposition:
                key_bytes = part.content
            elif 'name="cer"' in content_disposition:
                cer_bytes = part.content
            elif 'name="ctrsn"' in content_disposition:
                ctrsn = part.text

        if not key_bytes or not cer_bytes or not ctrsn:
            return create_response(400, "Faltan parámetros obligatorios (cer, key, ctrsn).")

        certificado_actual = db["certificates"].find_one({"_id": ObjectId(cert_id)})
        if not certificado_actual:
            return create_response(404, "Certificado no encontrado.")

        cert_x509 = x509.load_der_x509_certificate(cer_bytes, default_backend())
        subject = cert_x509.subject.rfc4514_string()
        rfc_match = re.search(r'2\.5\.4\.45=([A-Z0-9]+)', subject)
        rfc_nuevo = rfc_match.group(1) if rfc_match else None

        if certificado_actual.get("rfc") != rfc_nuevo:
            return create_response(400, "El RFC del nuevo certificado no coincide con el anterior.")

        serial_number = cert_x509.serial_number
        serial_bytes = serial_number.to_bytes((serial_number.bit_length() + 7) // 8, byteorder='big')
        serial_str = serial_bytes.decode('latin1')

        not_before = cert_x509.not_valid_before
        not_after = cert_x509.not_valid_after
        b64_key = base64.b64encode(key_bytes).decode("utf-8")
        b64_cer = base64.b64encode(cer_bytes).decode("utf-8")

        cert_body = {
            "type": "stamp",
            "b64Cer": b64_cer,
            "b64Key": b64_key,
            "password": ctrsn
        }

        token = get_sw_token()

        # Eliminar el certificado anterior en SW Sapien
        requests.delete(
            f"{SW_URL}/certificates/" + certificado_actual["no_certificado"],
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}"
            }
        ).json()

        # Guardar el nuevo certificado
        response = requests.post(
            f"{SW_URL}/certificates/save",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}"
            },
            data=json.dumps(cert_body)
        ).json()

        if response.get("messageDetail"):
            return create_response(400, response.get("messageDetail"))

        update_data = {
            "no_certificado": serial_str,
            "desde": not_before,
            "hasta": not_after,
            "updatedAt": datetime.utcnow()
        }

        db["certificates"].update_one({"_id": ObjectId(cert_id)}, {"$set": update_data})

        return create_response(200, "Certificado de plataforma actualizado correctamente.")
    except Exception as e:
        logger.exception("Error al actualizar certificado de plataforma")
        return handle_exception(e)


def delete_platform_certificate_handler(event, context):
    """DELETE /admin/facturacion/certificados/{id}
    Elimina un CSD de la plataforma de SW Sapien y de _platform['certificates'].
    """
    try:
        claims = get_claims(event)
        if not is_super_admin(claims):
            return create_response(403, "Solo un SUPER_ADMIN puede eliminar certificados de plataforma.")

        cert_id = event.get('pathParameters', {}).get('id')
        if not cert_id:
            return create_response(400, "ID de certificado no proporcionado.")

        db = get_platform_db()
        certificate = db["certificates"].find_one({"_id": ObjectId(cert_id)})
        if not certificate:
            return create_response(404, "Certificado no encontrado.")

        token = get_sw_token()
        requests.delete(
            f"{SW_URL}/certificates/" + certificate["no_certificado"],
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}"
            }
        ).json()

        db["certificates"].delete_one({"_id": ObjectId(cert_id)})
        # Limpiar referencias en la sucursal de plataforma
        db["sucursales"].update_many(
            {"id_certificado": cert_id},
            {"$unset": {"id_certificado": ""}}
        )

        return create_response(200, "Certificado de plataforma eliminado.")
    except Exception as e:
        logger.exception("Error al eliminar certificado de plataforma")
        return handle_exception(e)


# ==============================================================================
# 2. Configuración de Emisor / Sucursal / Folio para la Empresa
# ==============================================================================

def get_platform_emisor_config_handler(event, context):
    """GET /admin/facturacion/configuracion
    Obtiene la configuración fiscal de la empresa emisora y la secuencia actual de folios.
    """
    try:
        claims = get_claims(event)
        if not is_super_admin(claims):
            return create_response(403, "Acceso no autorizado.")

        db = get_platform_db()
        sucursal = db["sucursales"].find_one({"is_platform": True})
        if not sucursal:
            sucursal = db["sucursales"].find_one() or {}

        folio_doc = db["folios"].find_one({"tipo": "factura_platform"}) or {}

        config_data = {
            "id": str(sucursal.get("_id", "")),
            "nombre": sucursal.get("nombre", ""),
            "rfc": sucursal.get("rfc", ""),
            "regimen_fiscal": sucursal.get("regimen_fiscal", ""),
            "codigo_postal": sucursal.get("codigo_postal", ""),
            "direccion": sucursal.get("direccion", ""),
            "serie": sucursal.get("serie", "F"),
            "id_certificado": sucursal.get("id_certificado", ""),
            "folio_actual": folio_doc.get("secuencia", 0)
        }

        return create_response(200, "Configuración obtenida", config_data)
    except Exception as e:
        logger.exception("Error al obtener configuración de emisor de plataforma")
        return handle_exception(e)


def save_platform_emisor_config_handler(event, context):
    """POST /admin/facturacion/configuracion
    Guarda o actualiza los datos fiscales de la empresa emisora, serie, folio y CSD asignado.
    """
    try:
        claims = get_claims(event)
        if not is_super_admin(claims):
            return create_response(403, "Solo un SUPER_ADMIN puede actualizar la configuración fiscal.")

        body = json.loads(event.get("body") or "{}")
        db = get_platform_db()

        nombre = body.get("nombre", "").strip()
        rfc = body.get("rfc", "").strip().upper()
        regimen_fiscal = body.get("regimen_fiscal", "").strip()
        codigo_postal = body.get("codigo_postal", "").strip()
        serie = body.get("serie", "F").strip().upper()
        id_certificado = body.get("id_certificado", "")
        direccion = body.get("direccion", "").strip()

        if not rfc or not regimen_fiscal or not codigo_postal:
            return create_response(400, "RFC, Régimen Fiscal y Código Postal son obligatorios.")

        sucursal_doc = {
            "nombre": nombre or "Mekanics Manager",
            "rfc": rfc,
            "regimen_fiscal": regimen_fiscal,
            "codigo_postal": codigo_postal,
            "serie": serie,
            "id_certificado": id_certificado,
            "direccion": direccion,
            "is_platform": True,
            "activa": True,
            "updatedAt": datetime.utcnow()
        }

        res = db["sucursales"].find_one_and_update(
            {"is_platform": True},
            {"$set": sucursal_doc, "$setOnInsert": {"createdAt": datetime.utcnow()}},
            upsert=True,
            return_document=ReturnDocument.AFTER
        )

        folio_inicial = body.get("folio_inicial")
        if folio_inicial is not None:
            try:
                folio_num = int(folio_inicial)
                db["folios"].update_one(
                    {"tipo": "factura_platform"},
                    {"$set": {"secuencia": folio_num}},
                    upsert=True
                )
            except ValueError:
                pass

        sucursal_doc["id"] = str(res.get("_id", ""))
        return create_response(200, "Configuración guardada exitosamente", sucursal_doc)

    except Exception as e:
        logger.exception("Error al guardar configuración fiscal de plataforma")
        return handle_exception(e)


# ==============================================================================
# 3. Emisión de Factura de Suscripción (Cobro a Taller)
# ==============================================================================

def facturar_pago_suscripcion_handler(event, context):
    """POST /admin/facturacion/facturar-pago
    Timbra una factura CFDI 4.0 a nombre del taller por un pago de suscripción registrado.
    Body:
      - pagoId: ID del documento en suscripciones_pagos
      - tallerTenantId (opcional): tenantId del taller
    """
    try:
        claims = get_claims(event)
        if not is_super_admin(claims):
            return create_response(403, "Solo un SUPER_ADMIN puede emitir facturas de suscripción.")

        body = json.loads(event.get("body") or "{}")
        pago_id = body.get("pagoId")
        if not pago_id:
            return create_response(400, "El campo 'pagoId' es requerido.")

        db = get_platform_db()

        # 1. Obtener el registro de pago
        try:
            pago = db["suscripciones_pagos"].find_one({"_id": ObjectId(pago_id)})
        except Exception:
            return create_response(400, "ID de pago inválido.")

        if not pago:
            return create_response(404, "Pago no encontrado.")

        if pago.get("estado") != "COMPLETADO":
            return create_response(400, f"No se puede facturar un pago con estado '{pago.get('estado')}'. Debe estar COMPLETADO.")

        if pago.get("facturado"):
            return create_response(400, f"Este pago ya fue facturado previamente (UUID: {pago.get('uuid')}).")

        # 2. Obtener el taller
        taller_tenant_id = pago.get("tallerTenantId") or body.get("tallerTenantId")
        taller = db["talleres"].find_one({"tenantId": taller_tenant_id})
        if not taller:
            return create_response(404, "Taller asociado al pago no encontrado.")

        # 3. Validar datos fiscales del taller (Receptor)
        datos_fiscales = taller.get("datosFiscales") or {}
        rfc_receptor = (datos_fiscales.get("rfc") or "").strip().upper()
        nombre_receptor = (datos_fiscales.get("razonSocial") or taller.get("nombreComercial") or "").strip().upper()
        cp_receptor = (datos_fiscales.get("codigoPostal") or "").strip()
        regimen_receptor = (datos_fiscales.get("regimenFiscal") or "").strip()
        uso_cfdi = (datos_fiscales.get("usoCfdi") or "G03").strip().upper()

        if not rfc_receptor or not cp_receptor or not regimen_receptor:
            return create_response(400, (
                "El taller no cuenta con datos fiscales completos para facturar. "
                "Por favor edita el taller e ingresa RFC, Código Postal y Régimen Fiscal."
            ))

        # 4. Obtener configuración de emisor de la plataforma y su CSD
        sucursal = db["sucursales"].find_one({"is_platform": True})
        if not sucursal:
            sucursal = db["sucursales"].find_one() or {}

        emisor_rfc = (sucursal.get("rfc") or "").strip().upper()
        emisor_nombre = (sucursal.get("nombre") or "MEKANICS MANAGER").strip().upper()
        emisor_regimen = (sucursal.get("regimen_fiscal") or "").strip()
        lugar_expedicion = (sucursal.get("codigo_postal") or "").strip()
        serie = sucursal.get("serie", "F")
        id_certificado = sucursal.get("id_certificado")

        if not emisor_rfc or not emisor_regimen or not lugar_expedicion:
            return create_response(400, (
                "La configuración fiscal de la empresa emisora está incompleta. "
                "Ingresa a 'Clientes Facturas' -> 'Configuración Emisor' y completa los datos."
            ))

        # 5. Obtener CSD
        cert_doc = None
        if id_certificado:
            try:
                cert_doc = db["certificates"].find_one({"_id": ObjectId(id_certificado)})
            except Exception:
                pass
        if not cert_doc:
            cert_doc = db["certificates"].find_one()

        if not cert_doc:
            return create_response(400, "No hay ningún Certificado de Sello Digital (CSD) configurado en la plataforma.")

        # 6. Incrementar secuencia de folio en _platform['folios']
        folio_doc = db["folios"].find_one_and_update(
            {"tipo": "factura_platform"},
            {"$inc": {"secuencia": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER
        )
        secuencia = folio_doc.get("secuencia", 1)

        # 7. Calcular montos e impuestos (IVA 16% desglosado)
        total_pago = float(pago.get("monto") or 0.0)
        subtotal_val = round(total_pago / 1.16, 2)
        iva_val = round(total_pago - subtotal_val, 2)

        # Mapeo de forma de pago
        metodo_pago_raw = str(pago.get("metodo", "")).upper()
        if "CARD" in metodo_pago_raw or "TARJETA" in metodo_pago_raw or pago.get("tokenClip"):
            forma_pago_sat = "04"
        elif "SPEI" in metodo_pago_raw or "TRANSFER" in metodo_pago_raw:
            forma_pago_sat = "03"
        else:
            forma_pago_sat = "04"

        try:
            now_cdmx = datetime.now(ZoneInfo("America/Mexico_City"))
        except Exception:
            now_cdmx = datetime.utcnow()
        fecha_emision = now_cdmx.strftime("%Y-%m-%dT%H:%M:%S")

        # 8. Construir payload de timbrado CFDI 4.0
        timbrado_payload = {
            "Version": "4.0",
            "Serie": serie,
            "Folio": secuencia,
            "Fecha": fecha_emision,
            "FormaPago": forma_pago_sat,
            "CondicionesDePago": "Pago en una sola exhibición",
            "SubTotal": subtotal_val,
            "Descuento": 0.00,
            "Moneda": "MXN",
            "TipoCambio": 1,
            "Total": total_pago,
            "TipoDeComprobante": "I",
            "Exportacion": "01",
            "MetodoPago": "PUE",
            "LugarExpedicion": lugar_expedicion,
            "Emisor": {
                "Rfc": emisor_rfc,
                "Nombre": emisor_nombre,
                "RegimenFiscal": emisor_regimen
            },
            "Receptor": {
                "Rfc": rfc_receptor,
                "Nombre": nombre_receptor,
                "DomicilioFiscalReceptor": cp_receptor,
                "RegimenFiscalReceptor": regimen_receptor,
                "UsoCFDI": uso_cfdi
            },
            "Conceptos": [
                {
                    "ClaveProdServ": FACTURA_CLAVE_PROD_SERV,
                    "NoIdentificacion": "1",
                    "Cantidad": 1,
                    "ClaveUnidad": FACTURA_CLAVE_UNIDAD,
                    "Unidad": FACTURA_UNIDAD,
                    "Descripcion": FACTURA_DESCRIPCION,
                    "ValorUnitario": subtotal_val,
                    "Importe": subtotal_val,
                    "Descuento": 0.00,
                    "ObjetoImp": FACTURA_OBJETO_IMP,
                    "Impuestos": {
                        "Traslados": [
                            {
                                "Base": subtotal_val,
                                "Impuesto": "002",
                                "TipoFactor": "Tasa",
                                "TasaOCuota": "0.160000",
                                "Importe": iva_val
                            }
                        ]
                    }
                }
            ],
            "Impuestos": {
                "Traslados": [
                    {
                        "Base": subtotal_val,
                        "Impuesto": "002",
                        "TipoFactor": "Tasa",
                        "TasaOCuota": "0.160000",
                        "Importe": iva_val
                    }
                ],
                "TotalImpuestosTrasladados": iva_val
            }
        }

        # 9. Enviar a SW Sapien
        sw_token = get_sw_token()
        issue_headers = {
            "Content-Type": "application/jsontoxml",
            "Authorization": f"Bearer {sw_token}"
        }

        res_sw = requests.post(
            f"{SW_URL}/v3/cfdi33/issue/json/v4",
            headers=issue_headers,
            data=json.dumps(timbrado_payload)
        )

        if res_sw.status_code != 200:
            db["folios"].find_one_and_update(
                {"tipo": "factura_platform"},
                {"$inc": {"secuencia": -1}}
            )
            return create_response(res_sw.status_code, f"Error del PAC SW Sapien: {res_sw.text}")

        factura_res = res_sw.json()
        if factura_res.get("status") == "error":
            db["folios"].find_one_and_update(
                {"tipo": "factura_platform"},
                {"$inc": {"secuencia": -1}}
            )
            return create_response(400, factura_res.get("message") or "Error al emitir factura ante el SAT")

        # 10. Formatear XML
        cfdi_raw = factura_res["data"]["cfdi"]
        dom = xml.dom.minidom.parseString(cfdi_raw)
        pretty_xml = dom.toprettyxml(indent="  ", encoding="UTF-8").decode("utf-8")

        # 11. Guardar en _platform['facturasemitidas']
        factura_doc = {
            "cadenaOriginalSAT": factura_res["data"].get("cadenaOriginalSAT"),
            "cfdi": pretty_xml,
            "fechaTimbrado": factura_res["data"].get("fechaTimbrado"),
            "noCertificadoCFDI": factura_res["data"].get("noCertificadoCFDI"),
            "noCertificadoSAT": factura_res["data"].get("noCertificadoSAT"),
            "qrCode": factura_res["data"].get("qrCode"),
            "selloCFDI": factura_res["data"].get("selloCFDI"),
            "selloSAT": factura_res["data"].get("selloSAT"),
            "uuid": factura_res["data"].get("uuid"),
            "pagoId": str(pago["_id"]),
            "tallerTenantId": taller_tenant_id,
            "tallerNombre": taller.get("nombreComercial", ""),
            "serie": serie,
            "folio": secuencia,
            "tipo_de_comprobante": "I",
            "rfc_receptor": rfc_receptor,
            "nombre_receptor": nombre_receptor,
            "uso_cfdi": uso_cfdi,
            "regimen_fiscal_receptor": regimen_receptor,
            "domicilio_fiscal_receptor": cp_receptor,
            "forma_pago": forma_pago_sat,
            "metodo_pago": "PUE",
            "moneda": "MXN",
            "subtotal": subtotal_val,
            "total": total_pago,
            "estatus": "Vigente",
            "is_platform": True,
            "createdAt": datetime.utcnow()
        }

        res_insert = db["facturasemitidas"].insert_one(factura_doc)
        factura_id = str(res_insert.inserted_id)

        # 12. Actualizar estado en suscripciones_pagos
        db["suscripciones_pagos"].update_one(
            {"_id": pago["_id"]},
            {"$set": {
                "facturado": True,
                "facturaId": factura_id,
                "uuid": factura_res["data"].get("uuid"),
                "fechaFacturacion": datetime.utcnow()
            }}
        )

        # 13. Generar PDF
        pdf_b64 = None
        try:
            pdf_gen = CFDIPDF_FPDF_Generator(
                xml_string=pretty_xml,
                qrCode=factura_res["data"].get("qrCode") or "",
                cadena_original_sat=factura_res["data"].get("cadenaOriginalSAT") or "",
                noTicket=f"PAGO-{str(pago['_id'])[:8]}",
                fecha_hora_venta=pago.get("fechaPago"),
                direccion=sucursal.get("direccion", ""),
                empresa=emisor_nombre,
                regimen_fiscal_emisor=emisor_regimen,
                regimen_fiscal_receptor=regimen_receptor
            )
            pdf_bytes = pdf_gen.generate_pdf()
            pdf_b64 = base64.b64encode(pdf_bytes).decode("utf-8")
        except Exception as pdf_err:
            logger.warning(f"No se pudo generar PDF de la factura inmediatamente: {pdf_err}")

        factura_doc["_id"] = factura_id
        factura_doc["createdAt"] = iso_utc(factura_doc["createdAt"])
        if pdf_b64:
            factura_doc["pdf_cfdi_b64"] = pdf_b64

        return create_response(201, "Factura emitida exitosamente", factura_doc)

    except Exception as e:
        logger.exception("Error al facturar pago de suscripción")
        return handle_exception(e)


# ==============================================================================
# 4. Listado de Facturas Emitidas de la Empresa y Descarga de PDF
# ==============================================================================

def list_platform_facturas_handler(event, context):
    """GET /admin/facturacion/facturas
    Lista las facturas emitidas por la plataforma a los talleres.
    Filtra por month y year (opcionales) y soporta paginación.
    """
    try:
        claims = get_claims(event)
        if not is_super_admin(claims):
            return create_response(403, "Acceso no autorizado.")

        params = event.get("queryStringParameters") or {}
        month = params.get("month")
        year = params.get("year")
        page = int(params.get("page", 1))
        limit = int(params.get("limit", 10))
        skip = (page - 1) * limit

        query = {}
        if month and year:
            try:
                m = int(month)
                y = int(year)
                start_date = datetime(y, m, 1)
                if m == 12:
                    end_date = datetime(y + 1, 1, 1)
                else:
                    end_date = datetime(y, m + 1, 1)
                query["createdAt"] = {"$gte": start_date, "$lt": end_date}
            except ValueError:
                pass

        db = get_platform_db()
        total = db["facturasemitidas"].count_documents(query)
        total_pages = max(1, (total + limit - 1) // limit)

        cursor = db["facturasemitidas"].find(query).sort("createdAt", -1).skip(skip).limit(limit)

        facturas = []
        for doc in cursor:
            facturas.append({
                "id": str(doc["_id"]),
                "uuid": doc.get("uuid"),
                "folio": doc.get("folio"),
                "serie": doc.get("serie"),
                "ticket": doc.get("ticket") or f"PAGO-{doc.get('pagoId', '')[:8]}",
                "tallerNombre": doc.get("tallerNombre", ""),
                "rfc_receptor": doc.get("rfc_receptor", ""),
                "nombre_receptor": doc.get("nombre_receptor", ""),
                "fechaTimbrado": doc.get("fechaTimbrado"),
                "total": doc.get("total", 0.0),
                "subtotal": doc.get("subtotal", 0.0),
                "forma_pago": doc.get("forma_pago", ""),
                "metodo_pago": doc.get("metodo_pago", ""),
                "moneda": doc.get("moneda", "MXN"),
                "estatus": doc.get("estatus", "Vigente"),
                "cfdi": doc.get("cfdi"),
                "createdAt": iso_utc(doc.get("createdAt"))
            })

        return create_response(200, "Facturas emitidas de plataforma", {
            "items": facturas,
            "total": total,
            "page": page,
            "limit": limit,
            "totalPages": total_pages
        })

    except Exception as e:
        logger.exception("Error al listar facturas de plataforma")
        return handle_exception(e)


def get_platform_factura_pdf_handler(event, context):
    """GET /admin/facturacion/facturas/{id}/pdf
    Genera y retorna el PDF en base64 de una factura emitida por la plataforma.
    """
    try:
        claims = get_claims(event)
        if not is_super_admin(claims):
            return create_response(403, "Acceso no autorizado.")

        factura_id = event.get("pathParameters", {}).get("id")
        if not factura_id:
            return create_response(400, "ID de factura no proporcionado.")

        db = get_platform_db()
        try:
            factura = db["facturasemitidas"].find_one({"_id": ObjectId(factura_id)})
        except Exception:
            return create_response(400, "ID de factura inválido.")

        if not factura:
            return create_response(404, "Factura no encontrada.")

        cfdi_xml = factura.get("cfdi")
        if not cfdi_xml:
            return create_response(400, "La factura no contiene el XML CFDI.")

        sucursal = db["sucursales"].find_one({"is_platform": True}) or db["sucursales"].find_one() or {}

        pdf_gen = CFDIPDF_FPDF_Generator(
            xml_string=cfdi_xml,
            qrCode=factura.get("qrCode") or "",
            cadena_original_sat=factura.get("cadenaOriginalSAT") or "",
            noTicket=factura.get("ticket") or f"PAGO-{factura.get('pagoId', '')[:8]}",
            fecha_hora_venta=factura.get("fechaTimbrado") or "",
            direccion=sucursal.get("direccion", ""),
            empresa=sucursal.get("nombre", "MEKANICS MANAGER"),
            regimen_fiscal_emisor=sucursal.get("regimen_fiscal", ""),
            regimen_fiscal_receptor=factura.get("regimen_fiscal_receptor", "")
        )
        pdf_bytes = pdf_gen.generate_pdf()
        pdf_b64 = base64.b64encode(pdf_bytes).decode("utf-8")

        return create_response(200, "PDF generado correctamente", {"pdf_cfdi_b64": pdf_b64})

    except Exception as e:
        logger.exception("Error al generar PDF de factura de plataforma")
        return handle_exception(e)
