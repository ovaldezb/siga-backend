import json
import os
import requests
import xml.dom.minidom
import xml.etree.ElementTree as ET
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

def extraer_datos_cfdi(cfdi_xml_str):
    """Extrae datos fiscales clave de un XML de CFDI (3.3 o 4.0)."""
    try:
        if not cfdi_xml_str or not isinstance(cfdi_xml_str, str):
            return {}
        
        root = ET.fromstring(cfdi_xml_str.encode('utf-8'))
        
        serie = root.attrib.get('Serie') or root.attrib.get('serie') or ''
        folio = root.attrib.get('Folio') or root.attrib.get('folio') or ''
        subtotal_str = root.attrib.get('SubTotal') or root.attrib.get('subTotal') or '0'
        total_str = root.attrib.get('Total') or root.attrib.get('total') or '0'
        moneda = root.attrib.get('Moneda') or root.attrib.get('moneda') or 'MXN'
        forma_pago = root.attrib.get('FormaPago') or root.attrib.get('formaPago') or ''
        metodo_pago = root.attrib.get('MetodoPago') or root.attrib.get('metodoPago') or ''

        try:
            subtotal = float(subtotal_str)
        except (ValueError, TypeError):
            subtotal = 0.0

        try:
            total = float(total_str)
        except (ValueError, TypeError):
            total = 0.0

        rfc_receptor = ''
        nombre_receptor = ''
        uso_cfdi = ''
        regimen_fiscal_receptor = ''

        for elem in root.iter():
            tag = elem.tag.split('}')[-1] if '}' in elem.tag else elem.tag
            if tag == 'Receptor':
                rfc_receptor = elem.attrib.get('Rfc') or elem.attrib.get('rfc') or ''
                nombre_receptor = elem.attrib.get('Nombre') or elem.attrib.get('nombre') or ''
                uso_cfdi = elem.attrib.get('UsoCFDI') or elem.attrib.get('usoCFDI') or ''
                regimen_fiscal_receptor = elem.attrib.get('RegimenFiscalReceptor') or elem.attrib.get('regimenFiscalReceptor') or ''
                break

        return {
            "serie": serie,
            "folio": folio,
            "subtotal": subtotal,
            "total": total,
            "moneda": moneda,
            "forma_pago": forma_pago,
            "metodo_pago": metodo_pago,
            "rfc_receptor": rfc_receptor,
            "nombre_receptor": nombre_receptor,
            "uso_cfdi": uso_cfdi,
            "regimen_fiscal_receptor": regimen_fiscal_receptor
        }
    except Exception as e:
        logger.warning(f"Error parseando XML CFDI: {e}")
        return {}

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
        receptor = timbrado.get("Receptor") or {}
        try:
            subtotal_val = float(timbrado.get("SubTotal") or 0.0)
        except (ValueError, TypeError):
            subtotal_val = 0.0

        try:
            total_val = float(timbrado.get("Total") or 0.0)
        except (ValueError, TypeError):
            total_val = 0.0

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
            "serie": timbrado.get("Serie") or "",
            "folio": secuencia,
            "rfc_receptor": receptor.get("Rfc") or "",
            "nombre_receptor": receptor.get("Nombre") or "",
            "uso_cfdi": receptor.get("UsoCFDI") or "",
            "regimen_fiscal_receptor": receptor.get("RegimenFiscalReceptor") or "",
            "forma_pago": timbrado.get("FormaPago") or "",
            "metodo_pago": timbrado.get("MetodoPago") or "",
            "moneda": timbrado.get("Moneda") or "MXN",
            "subtotal": subtotal_val,
            "total": total_val,
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

        # Formatear para JSON y auto-migración híbrida (Just-In-Time)
        for f in facturas:
            f_id = f.pop('_id')
            f['id'] = str(f_id)
            if 'createdAt' in f and hasattr(f['createdAt'], 'isoformat'):
                f['createdAt'] = f['createdAt'].isoformat()

            # Si es un documento histórico y no tiene campos desnormalizados
            if 'nombre_receptor' not in f or 'total' not in f or f.get('nombre_receptor') is None:
                cfdi_xml = f.get('cfdi')
                if cfdi_xml:
                    datos_extraidos = extraer_datos_cfdi(cfdi_xml)
                    if datos_extraidos:
                        # Completar en memoria para respuesta inmediata
                        for k, v in datos_extraidos.items():
                            if k not in f or f.get(k) is None:
                                f[k] = v
                        
                        # Persistir en BD para que la próxima lectura sea directa y nativa
                        try:
                            db["facturasemitidas"].update_one(
                                {"_id": f_id},
                                {"$set": datos_extraidos}
                            )
                        except Exception as update_err:
                            logger.warning(f"No se pudo auto-migrar documento de factura {f_id}: {update_err}")

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

def get_factura_pdf_handler(event, context):
    """GET /facturas/{id}/pdf — Genera y retorna el PDF de una factura en base64."""
    try:
        claims = get_claims(event)
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        factura_id = event.get('pathParameters', {}).get('id')
        if not factura_id:
            return create_response(400, "ID de factura no proporcionado")

        try:
            factura_oid = ObjectId(factura_id)
        except Exception:
            return create_response(400, "ID de factura inválido")

        db = get_tenant_db(tenant_id)
        factura = db["facturasemitidas"].find_one({"_id": factura_oid})
        if not factura:
            return create_response(404, "Factura no encontrada")

        cfdi_xml = factura.get("cfdi")
        if not cfdi_xml:
            return create_response(400, "La factura no contiene el XML CFDI")

        # 1. Parsear XML para obtener el Régimen Fiscal y Datos de Emisor
        import xml.etree.ElementTree as ET
        try:
            # Eliminar caracteres extraños si existen antes del parse
            if cfdi_xml.startswith('\ufeff'):
                cfdi_xml = cfdi_xml[1:]
            root = ET.fromstring(cfdi_xml)
        except Exception as parse_err:
            logger.error(f"Error parseando XML para PDF: {parse_err}")
            return create_response(500, "El XML de la factura está malformado")

        # Namespaces de CFDI v4
        ns = {'cfdi': 'http://www.sat.gob.mx/cfd/4'}
        
        emisor_node = root.find('cfdi:Emisor', ns)
        receptor_node = root.find('cfdi:Receptor', ns)

        emisor_regimen = ""
        emisor_nombre = ""
        if emisor_node is not None:
            emisor_regimen = emisor_node.attrib.get("RegimenFiscal", "")
            emisor_nombre = emisor_node.attrib.get("Nombre", "")

        receptor_regimen = ""
        if receptor_node is not None:
            receptor_regimen = receptor_node.attrib.get("RegimenFiscalReceptor", "")

        # 2. Buscar descripciones de Régimen Fiscal
        reg_emisor_doc = db["regimenfiscal"].find_one({"regimenfiscal": emisor_regimen})
        regimen_fiscal_emisor = reg_emisor_doc.get("descripcion") if reg_emisor_doc else emisor_regimen

        reg_receptor_doc = db["regimenfiscal"].find_one({"regimenfiscal": receptor_regimen})
        regimen_fiscal_receptor = reg_receptor_doc.get("descripcion") if reg_receptor_doc else receptor_regimen

        # 3. Obtener dirección de la sucursal
        sucursal_id = factura.get("sucursal")
        direccion = ""
        if sucursal_id:
            try:
                suc_oid = ObjectId(sucursal_id)
                sucursal = db["sucursales"].find_one({"_id": suc_oid})
                if sucursal:
                    direccion = sucursal.get("direccion", "")
            except Exception:
                pass

        # 4. Obtener logo de S3
        logo_path = None
        logo_temp_file = None
        try:
            platform_db = get_platform_db()
            taller = platform_db["talleres"].find_one({"tenantId": tenant_id})
            logo_url = taller.get("logoUrl") if taller else None

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
                    logger.warning(f"No se pudo descargar el logotipo para el PDF: {img_err}")
        except Exception:
            pass

        # 5. Generar PDF
        pdf_b64 = None
        try:
            # Obtener datos adicionales almacenados
            qr_code = factura.get("qrCode") or ""
            cadena_original_sat = factura.get("cadenaOriginalSAT") or ""
            ticket = factura.get("ticket") or ""
            fecha_venta = factura.get("fechaTimbrado") or ""
            empresa = emisor_nombre or factura.get("idCertificado") or ""

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
        except Exception as gen_err:
            logger.exception("Error al generar PDF")
            return create_response(500, f"Error al generar PDF de la factura: {str(gen_err)}")
        finally:
            if logo_temp_file and os.path.exists(logo_temp_file.name):
                try:
                    os.unlink(logo_temp_file.name)
                except Exception:
                    pass

        return create_response(200, "PDF generado exitosamente", {
            "pdf_cfdi_b64": pdf_b64
        })

    except Exception as e:
        logger.exception("Error en get_factura_pdf")
        return handle_exception(e)
