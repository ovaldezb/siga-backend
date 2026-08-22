import json
import os
import requests
import xml.dom.minidom
from datetime import datetime
from bson import ObjectId
from pymongo import ReturnDocument

from aws_lambda_powertools import Logger
from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.infrastructure.database import get_tenant_db, get_platform_db
from src.shared.utils.auth_utils import get_claims, parse_object_id
from src.handlers.facturacion.certificates_manager import get_sw_token
from src.handlers.facturacion.cfdi_pdf_fpdf_generator import CFDIPDF_FPDF_Generator
import base64
import tempfile

logger = Logger()

SW_URL = os.getenv("SW_URL")

def timbrar_factura_handler(event, context):
    """POST /timbrar-factura — Timbra un CFDI 4.0 con SW Sapien."""
    try:
        claims = get_claims(event)
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        body = json.loads(event.get('body', '{}'))
        timbrado = body.get('timbrado')
        sucursal_id = body.get('sucursal')
        ticket = body.get('ticket')
        id_certificado = body.get('idCertificado')
        fecha_venta = body.get('fechaVenta')
        email_receptor = body.get('email')

        if not timbrado or not sucursal_id or not id_certificado:
            return create_response(400, "Faltan parámetros requeridos (timbrado, sucursal, idCertificado).")

        db = get_tenant_db(tenant_id)

        # 1. Obtener la sucursal para verificar y obtener datos
        suc_oid, err = parse_object_id(sucursal_id)
        if err:
            return create_response(400, f"ID de sucursal inválido: {err}")
        sucursal = db["sucursales"].find_one({"_id": suc_oid})
        if not sucursal:
            return create_response(404, "No se encontró la sucursal.")

        # 2. Incrementar folio en folios de facturación de la sucursal
        folio_doc = db["folios"].find_one_and_update(
            {"tipo": "factura", "sucursal_id": sucursal_id},
            {"$inc": {"secuencia": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER
        )
        secuencia = folio_doc.get("secuencia", 1)

        # 3. Asignar Folio y Serie
        timbrado['Folio'] = secuencia
        timbrado['Serie'] = sucursal.get('serie') or 'F'

        # 4. Obtener token de autenticación
        sw_token = get_sw_token()

        # 5. Enviar el timbrado a SW Sapien (JSON a CFDI v4.0)
        issue_headers = {
            "Content-Type": "application/jsontoxml",
            "Authorization": f"Bearer {sw_token}"
        }
        
        response_sw = requests.post(
            f"{SW_URL}/v3/cfdi33/issue/json/v4",
            headers=issue_headers,
            data=json.dumps(timbrado)
        )
        
        if response_sw.status_code != 200:
            # Fallo de comunicación o error de SW
            # Decrementar folio para no dejar huecos
            db["folios"].find_one_and_update(
                {"tipo": "factura", "sucursal_id": sucursal_id},
                {"$inc": {"secuencia": -1}}
            )
            return create_response(response_sw.status_code, f"Error del PAC: {response_sw.text}")

        factura_generada = response_sw.json()
        if factura_generada.get("status") == 'error':
            # Decrementar folio
            db["folios"].find_one_and_update(
                {"tipo": "factura", "sucursal_id": sucursal_id},
                {"$inc": {"secuencia": -1}}
            )
            return create_response(400, factura_generada.get("message") or "Error al generar factura")

        # 6. Formatear XML
        cfdi_raw = factura_generada["data"]["cfdi"]
        dom = xml.dom.minidom.parseString(cfdi_raw)
        pretty_xml = dom.toprettyxml(indent="  ", encoding="UTF-8").decode("utf-8")

        # 7. Persistir en la colección facturasemitidas
        factura_doc = {
            "cadenaOriginalSAT": factura_generada["data"].get("cadenaOriginalSAT"),
            "cfdi": pretty_xml,
            "fechaTimbrado": factura_generada["data"].get("fechaTimbrado"),
            "noCertificadoCFDI": factura_generada["data"].get("noCertificadoCFDI"),
            "noCertificadoSAT": factura_generada["data"].get("noCertificadoSAT"),
            "qrCode": factura_generada["data"].get("qrCode"),
            "selloCFDI": factura_generada["data"].get("selloCFDI"),
            "selloSAT": factura_generada["data"].get("selloSAT"),
            "uuid": factura_generada["data"].get("uuid"),
            "sucursal": sucursal_id,
            "idCertificado": id_certificado,
            "ticket": ticket,
            "estatus": "Vigente",
            "tenant_id": tenant_id,
            "createdAt": datetime.utcnow()
        }
        db["facturasemitidas"].insert_one(factura_doc)

        # Actualizar estado de facturación en la venta
        venta_id = body.get("ventaId")
        if venta_id:
            db["ventas"].update_one(
                {"_id": ObjectId(venta_id)},
                {"$set": {"venta_facturada": True}}
            )
        else:
            db["ventas"].update_one(
                {"folio": ticket},
                {"$set": {"venta_facturada": True}}
            )

        # 7.5. Generar PDF de la factura
        pdf_b64 = None
        logo_temp_file = None
        try:
            # Obtener logoUrl del taller de la base de datos de plataforma
            platform_db = get_platform_db()
            taller = platform_db["talleres"].find_one({"tenantId": tenant_id})
            logo_url = taller.get("logoUrl") if taller else None

            # Descargar el logo de S3 a un archivo temporal
            logo_path = None
            if logo_url:
                try:
                    res_img = requests.get(logo_url, timeout=5)
                    if res_img.status_code == 200:
                        logo_temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
                        logo_temp_file.write(res_img.content)
                        logo_temp_file.flush()
                        logo_temp_file.close()
                        logo_path = logo_temp_file.name
                except Exception as img_err:
                    logger.warning(f"No se pudo descargar el logotipo del taller desde S3: {img_err}")

            # Obtener descripciones de régimen fiscal
            emisor_regimen = timbrado.get("Emisor", {}).get("RegimenFiscal") or sucursal.get("regimen_fiscal") or ""
            reg_emisor_doc = db["regimenfiscal"].find_one({"regimenfiscal": emisor_regimen})
            regimen_fiscal_emisor = reg_emisor_doc.get("descripcion") if reg_emisor_doc else emisor_regimen

            receptor_regimen = timbrado.get("Receptor", {}).get("RegimenFiscalReceptor") or ""
            reg_receptor_doc = db["regimenfiscal"].find_one({"regimenfiscal": receptor_regimen})
            regimen_fiscal_receptor = reg_receptor_doc.get("descripcion") if reg_receptor_doc else receptor_regimen

            # Generar PDF bytes
            cfdi_xml = factura_generada["data"]["cfdi"]
            qr_code = factura_generada["data"].get("qrCode") or ""
            cadena_original_sat = factura_generada["data"].get("cadenaOriginalSAT") or ""
            direccion = body.get("direccion", sucursal.get("direccion") or "")
            empresa = body.get("empresa", id_certificado)

            pdf_gen = CFDIPDF_FPDF_Generator(
                xml_string=cfdi_xml,
                qrCode=qr_code,
                cadena_original_sat=cadena_original_sat,
                noTicket=ticket,
                fecha_hora_venta=fecha_venta,
                direccion=direccion,
                empresa=empresa,
                regimen_fiscal_emisor=regimen_fiscal_emisor,
                regimen_fiscal_receptor=regimen_fiscal_receptor,
                logo_path=logo_path
            )
            pdf_bytes = pdf_gen.generate_pdf()
            pdf_b64 = base64.b64encode(pdf_bytes).decode('utf-8')

        except Exception as pdf_err:
            logger.exception(f"Ocurrió un error no fatal al generar el PDF de la factura: {pdf_err}")
        finally:
            if logo_temp_file and os.path.exists(logo_temp_file.name):
                try:
                    os.unlink(logo_temp_file.name)
                except Exception:
                    pass
        # 8. Retornar respuesta
        res_payload = {
            "cfdi": pretty_xml,
            "uuid": factura_generada["data"].get("uuid"),
            "folio": secuencia,
            "serie": timbrado['Serie'],
            "pdf_cfdi_b64": pdf_b64
        }
        return create_response(200, "Factura generada exitosamente", res_payload)

    except Exception as e:
        logger.exception("Error al timbrar factura")
        return handle_exception(e)

def list_facturas_handler(event, context):
    """GET /facturas — Lista facturas emitidas filtradas por mes/año y paginadas."""
    try:
        claims = get_claims(event)
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        query_params = event.get('queryStringParameters') or {}
        
        # Filtro de fecha (mes y año)
        now = datetime.utcnow()
        try:
            month = int(query_params.get('month', now.month))
            year = int(query_params.get('year', now.year))
        except (ValueError, TypeError):
            month = now.month
            year = now.year

        # Paginación
        try:
            page = int(query_params.get('page', 1))
            limit = int(query_params.get('limit', 10))
        except (ValueError, TypeError):
            page = 1
            limit = 10

        skip = (page - 1) * limit

        db = get_tenant_db(tenant_id)

        # Rango de fechas
        start_date = datetime(year, month, 1)
        if month == 12:
            end_date = datetime(year + 1, 1, 1)
        else:
            end_date = datetime(year, month + 1, 1)

        filter_query = {
            "createdAt": {
                "$gte": start_date,
                "$lt": end_date
            }
        }

        total = db["facturasemitidas"].count_documents(filter_query)
        facturas = list(
            db["facturasemitidas"]
            .find(filter_query)
            .sort("createdAt", -1)
            .skip(skip)
            .limit(limit)
        )

        # Formatear para JSON
        for f in facturas:
            f['id'] = str(f.pop('_id'))
            if 'createdAt' in f:
                f['createdAt'] = f['createdAt'].isoformat()

        return create_response(200, "Facturas obtenidas exitosamente", {
            "items": facturas,
            "total": total,
            "page": page,
            "limit": limit,
            "totalPages": (total + limit - 1) // limit if limit > 0 else 0
        })

    except Exception as e:
        logger.exception("Error al listar facturas")
        return handle_exception(e)
