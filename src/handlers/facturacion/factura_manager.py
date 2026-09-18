import json
import os
import requests
import xml.dom.minidom
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
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
        
        if cfdi_xml_str.startswith('\ufeff'):
            cfdi_xml_str = cfdi_xml_str[1:]

        root = ET.fromstring(cfdi_xml_str.encode('utf-8'))
        
        serie = root.attrib.get('Serie') or root.attrib.get('serie') or ''
        folio = root.attrib.get('Folio') or root.attrib.get('folio') or ''
        subtotal_str = root.attrib.get('SubTotal') or root.attrib.get('subTotal') or '0'
        total_str = root.attrib.get('Total') or root.attrib.get('total') or '0'
        moneda = root.attrib.get('Moneda') or root.attrib.get('moneda') or 'MXN'
        forma_pago = root.attrib.get('FormaPago') or root.attrib.get('formaPago') or ''
        metodo_pago = root.attrib.get('MetodoPago') or root.attrib.get('metodoPago') or ''
        tipo_comprobante = root.attrib.get('TipoDeComprobante') or root.attrib.get('tipoDeComprobante') or 'I'
        lugar_expedicion = root.attrib.get('LugarExpedicion') or root.attrib.get('lugarExpedicion') or ''

        try:
            subtotal = float(subtotal_str)
        except (ValueError, TypeError):
            subtotal = 0.0

        try:
            total = float(total_str)
        except (ValueError, TypeError):
            total = 0.0

        emisor_rfc = ''
        emisor_nombre = ''
        emisor_regimen = ''
        rfc_receptor = ''
        nombre_receptor = ''
        uso_cfdi = ''
        regimen_fiscal_receptor = ''
        domicilio_fiscal_receptor = ''

        for elem in root.iter():
            tag = elem.tag.split('}')[-1] if '}' in elem.tag else elem.tag
            if tag == 'Emisor':
                emisor_rfc = elem.attrib.get('Rfc') or elem.attrib.get('rfc') or ''
                emisor_nombre = elem.attrib.get('Nombre') or elem.attrib.get('nombre') or ''
                emisor_regimen = elem.attrib.get('RegimenFiscal') or elem.attrib.get('regimenFiscal') or ''
            elif tag == 'Receptor':
                rfc_receptor = elem.attrib.get('Rfc') or elem.attrib.get('rfc') or ''
                nombre_receptor = elem.attrib.get('Nombre') or elem.attrib.get('nombre') or ''
                uso_cfdi = elem.attrib.get('UsoCFDI') or elem.attrib.get('usoCFDI') or ''
                regimen_fiscal_receptor = elem.attrib.get('RegimenFiscalReceptor') or elem.attrib.get('regimenFiscalReceptor') or ''
                domicilio_fiscal_receptor = elem.attrib.get('DomicilioFiscalReceptor') or elem.attrib.get('domicilioFiscalReceptor') or ''

        return {
            "serie": serie,
            "folio": folio,
            "subtotal": subtotal,
            "total": total,
            "moneda": moneda,
            "forma_pago": forma_pago,
            "metodo_pago": metodo_pago,
            "tipo_de_comprobante": tipo_comprobante,
            "lugar_expedicion": lugar_expedicion,
            "emisor_rfc": emisor_rfc,
            "emisor_nombre": emisor_nombre,
            "emisor_regimen": emisor_regimen,
            "rfc_receptor": rfc_receptor,
            "nombre_receptor": nombre_receptor,
            "uso_cfdi": uso_cfdi,
            "regimen_fiscal_receptor": regimen_fiscal_receptor,
            "domicilio_fiscal_receptor": domicilio_fiscal_receptor
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

        metodo_pago_val = timbrado.get("MetodoPago") or ""
        domicilio_receptor = receptor.get("DomicilioFiscalReceptor") or timbrado.get("LugarExpedicion") or ""

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
            "tipo_de_comprobante": timbrado.get("TipoDeComprobante") or "I",
            "rfc_receptor": receptor.get("Rfc") or "",
            "nombre_receptor": receptor.get("Nombre") or "",
            "uso_cfdi": receptor.get("UsoCFDI") or "",
            "regimen_fiscal_receptor": receptor.get("RegimenFiscalReceptor") or "",
            "domicilio_fiscal_receptor": domicilio_receptor,
            "forma_pago": timbrado.get("FormaPago") or "",
            "metodo_pago": metodo_pago_val,
            "moneda": timbrado.get("Moneda") or "MXN",
            "subtotal": subtotal_val,
            "total": total_val,
            "saldo_insoluto": total_val if metodo_pago_val == "PPD" else 0.0,
            "esta_liquidada": False if metodo_pago_val == "PPD" else True,
            "ultima_parcialidad": 0,
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

            # Si es factura PPD, asegurar saldo_insoluto y esta_liquidada
            if f.get('metodo_pago') == 'PPD' and 'saldo_insoluto' not in f:
                comps = list(db["facturasemitidas"].find({
                    "factura_padre_id": str(f_id),
                    "estatus": {"$ne": "Cancelada"}
                }))
                pagado = sum(float(c.get("imp_pagado") or 0.0) for c in comps)
                tot = float(f.get("total") or 0.0)
                saldo = round(max(0.0, tot - pagado), 2)
                f['saldo_insoluto'] = saldo
                f['esta_liquidada'] = (saldo <= 0.001)
                f['ultima_parcialidad'] = len(comps)
                try:
                    db["facturasemitidas"].update_one(
                        {"_id": f_id},
                        {"$set": {
                            "saldo_insoluto": saldo,
                            "esta_liquidada": (saldo <= 0.001),
                            "ultima_parcialidad": len(comps)
                        }}
                    )
                except Exception as up_err:
                    logger.warning(f"No se pudo guardar saldo PPD de factura {f_id}: {up_err}")

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

def get_factura_complementos_handler(event, context):
    """GET /facturas/{id}/complementos — Obtiene el historial de complementos de pago y saldo de una factura PPD."""
    try:
        claims = get_claims(event)
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        factura_id = event.get('pathParameters', {}).get('id')
        if not factura_id:
            return create_response(400, "ID de factura no proporcionado.")

        try:
            factura_oid = ObjectId(factura_id)
        except Exception:
            return create_response(400, "ID de factura inválido.")

        db = get_tenant_db(tenant_id)
        factura = db["facturasemitidas"].find_one({"_id": factura_oid})
        if not factura:
            return create_response(404, "Factura no encontrada.")

        total_factura = float(factura.get("total") or 0.0)
        metodo_pago = factura.get("metodo_pago") or ""
        cfdi_xml = factura.get("cfdi")
        if (total_factura == 0.0 or not metodo_pago) and cfdi_xml:
            datos = extraer_datos_cfdi(cfdi_xml)
            if total_factura == 0.0:
                total_factura = float(datos.get("total") or 0.0)
            if not metodo_pago:
                metodo_pago = datos.get("metodo_pago") or ""

        # Obtener complementos emitidos ordenados por num_parcialidad
        complementos_cursor = db["facturasemitidas"].find({
            "factura_padre_id": str(factura_oid)
        }).sort("num_parcialidad", 1)

        complementos = []
        total_pagado = 0.0
        parcialidades_vigentes = 0

        for c in complementos_cursor:
            c_id = c.pop('_id')
            c['id'] = str(c_id)
            if 'createdAt' in c and hasattr(c['createdAt'], 'isoformat'):
                c['createdAt'] = c['createdAt'].isoformat()
            
            es_vigente = c.get('estatus') != 'Cancelada'
            if es_vigente:
                imp_pagado = float(c.get('imp_pagado') or 0.0)
                total_pagado += imp_pagado
                parcialidades_vigentes += 1
            
            complementos.append(c)

        saldo_insoluto = round(max(0.0, total_factura - total_pagado), 2)
        esta_liquidada = saldo_insoluto <= 0.001
        proxima_parcialidad = parcialidades_vigentes + 1

        # Actualizar en la factura padre si aplica
        db["facturasemitidas"].update_one(
            {"_id": factura_oid},
            {"$set": {
                "saldo_insoluto": saldo_insoluto,
                "esta_liquidada": esta_liquidada,
                "ultima_parcialidad": parcialidades_vigentes
            }}
        )

        res_payload = {
            "factura_padre": {
                "id": str(factura["_id"]),
                "uuid": factura.get("uuid"),
                "serie": factura.get("serie"),
                "folio": factura.get("folio"),
                "total": total_factura,
                "metodo_pago": metodo_pago,
                "rfc_receptor": factura.get("rfc_receptor"),
                "nombre_receptor": factura.get("nombre_receptor"),
                "estatus": factura.get("estatus", "Vigente")
            },
            "total_factura": total_factura,
            "total_pagado": round(total_pagado, 2),
            "saldo_insoluto": saldo_insoluto,
            "esta_liquidada": esta_liquidada,
            "proxima_parcialidad": proxima_parcialidad,
            "complementos": complementos
        }
        return create_response(200, "Historial de complementos obtenido exitosamente", res_payload)

    except Exception as e:
        logger.exception("Error al obtener complementos de pago")
        return handle_exception(e)

def timbrar_complemento_pago_handler(event, context):
    """POST /facturas/{id}/complemento-pago — Timbra un CFDI 4.0 con Complemento de Recepción de Pagos 2.0 (REP)."""
    try:
        claims = get_claims(event)
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        factura_id = event.get('pathParameters', {}).get('id')
        if not factura_id:
            return create_response(400, "ID de factura no proporcionado.")

        try:
            factura_oid = ObjectId(factura_id)
        except Exception:
            return create_response(400, "ID de factura inválido.")

        body = json.loads(event.get('body', '{}'))
        monto_raw = body.get('monto')
        forma_pago = str(body.get('forma_pago') or '03').strip()
        fecha_pago_raw = body.get('fecha_pago')
        num_operacion = body.get('num_operacion')
        sucursal_id = body.get('sucursal')
        id_certificado = body.get('idCertificado')

        if monto_raw is None:
            return create_response(400, "El campo 'monto' es requerido.")
        try:
            monto = float(monto_raw)
        except (ValueError, TypeError):
            return create_response(400, "El monto del pago es inválido.")

        if monto <= 0:
            return create_response(400, "El monto del pago debe ser mayor a 0.")

        if forma_pago == "99":
            return create_response(400, "La forma de pago en el complemento no puede ser '99 Por definir'. Debe seleccionar una forma de pago válida.")

        db = get_tenant_db(tenant_id)
        factura = db["facturasemitidas"].find_one({"_id": factura_oid})
        if not factura:
            return create_response(404, "Factura padre no encontrada.")

        if factura.get("estatus") == "Cancelada":
            return create_response(400, "No se puede emitir un complemento de pago para una factura cancelada.")

        if factura.get("tipo_de_comprobante") == "P":
            return create_response(400, "No se puede emitir un complemento de pago sobre otro comprobante de pago.")

        cfdi_padre_xml = factura.get("cfdi") or ""
        padre_datos = extraer_datos_cfdi(cfdi_padre_xml) if cfdi_padre_xml else {}

        metodo_pago_padre = factura.get("metodo_pago") or padre_datos.get("metodo_pago") or ""
        if metodo_pago_padre and metodo_pago_padre != "PPD":
            return create_response(400, f"El complemento de pago solo aplica para facturas emitidas con método de pago PPD (la factura fue emitida con {metodo_pago_padre}).")

        total_factura = float(factura.get("total") or padre_datos.get("total") or 0.0)
        if total_factura <= 0:
            return create_response(400, "La factura padre tiene un total inválido o en cero.")

        sucursal_id = sucursal_id or factura.get("sucursal")
        if not sucursal_id:
            return create_response(400, "No se pudo identificar la sucursal emisora.")

        suc_oid, err = parse_object_id(sucursal_id)
        if err:
            return create_response(400, f"ID de sucursal inválido: {err}")
        sucursal = db["sucursales"].find_one({"_id": suc_oid})
        if not sucursal:
            return create_response(404, "No se encontró la sucursal emisora.")

        id_certificado = id_certificado or factura.get("idCertificado") or ""

        # Consultar complementos previos vigentes
        complementos_previos = list(db["facturasemitidas"].find({
            "factura_padre_id": str(factura_oid),
            "estatus": {"$ne": "Cancelada"}
        }).sort("num_parcialidad", 1))

        total_pagado_previo = sum(float(c.get("imp_pagado") or 0.0) for c in complementos_previos)
        imp_saldo_ant = round(total_factura - total_pagado_previo, 2)

        if imp_saldo_ant <= 0.001:
            return create_response(400, "La factura ya se encuentra totalmente liquidada.")

        if round(monto, 2) > imp_saldo_ant + 0.01:
            return create_response(400, f"El monto del pago (${monto:.2f}) excede el saldo insoluto restante (${imp_saldo_ant:.2f}).")

        if monto > imp_saldo_ant:
            monto = imp_saldo_ant

        imp_saldo_insoluto = round(imp_saldo_ant - monto, 2)
        num_parcialidad = len(complementos_previos) + 1

        # Fechas en zona horaria local de México
        now_cdmx = datetime.now(ZoneInfo("America/Mexico_City"))
        fecha_emision = now_cdmx.strftime("%Y-%m-%dT%H:%M:%S")

        if fecha_pago_raw:
            f_str = str(fecha_pago_raw).strip()
            if len(f_str) == 10:
                fecha_pago = f"{f_str}T12:00:00"
            else:
                fecha_pago = f_str.replace('Z', '').split('+')[0]
        else:
            fecha_pago = fecha_emision

        # Obtener o incrementar folio de complemento de pago
        folio_doc = db["folios"].find_one_and_update(
            {"tipo": "complemento_pago", "sucursal_id": str(sucursal_id)},
            {"$inc": {"secuencia": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER
        )
        secuencia = folio_doc.get("secuencia", 1)
        serie = body.get('serie') or sucursal.get('serie_pago') or (f"P{sucursal.get('serie')}" if sucursal.get('serie') else "P")

        # Datos fiscales del emisor y receptor
        lugar_expedicion = padre_datos.get("lugar_expedicion") or sucursal.get("codigo_postal") or "00000"
        emisor_rfc = padre_datos.get("emisor_rfc") or sucursal.get("rfc") or ""
        emisor_nombre = padre_datos.get("emisor_nombre") or sucursal.get("nombre") or ""
        emisor_regimen = padre_datos.get("emisor_regimen") or sucursal.get("regimen_fiscal") or "601"

        receptor_rfc = factura.get("rfc_receptor") or padre_datos.get("rfc_receptor") or ""
        receptor_nombre = factura.get("nombre_receptor") or padre_datos.get("nombre_receptor") or ""
        receptor_domicilio = factura.get("domicilio_fiscal_receptor") or padre_datos.get("domicilio_fiscal_receptor") or lugar_expedicion
        receptor_regimen = factura.get("regimen_fiscal_receptor") or padre_datos.get("regimen_fiscal_receptor") or "616"

        # Verificar si la factura padre causó IVA 16%
        tiene_iva16 = False
        if cfdi_padre_xml:
            try:
                root_padre = ET.fromstring(cfdi_padre_xml.encode('utf-8'))
                for elem in root_padre.iter():
                    tag = elem.tag.split('}')[-1] if '}' in elem.tag else elem.tag
                    if tag == 'Traslado':
                        imp = elem.attrib.get('Impuesto') or ''
                        tasa = elem.attrib.get('TasaOCuota') or ''
                        if imp == '002' and (tasa.startswith('0.16') or tasa == '0.160000'):
                            tiene_iva16 = True
                            break
            except Exception:
                tiene_iva16 = True
        else:
            tiene_iva16 = True

        if tiene_iva16:
            base_dr = round(monto / 1.16, 6)
            importe_dr = round(monto - base_dr, 6)
            total_base_16 = round(monto / 1.16, 2)
            total_imp_16 = round(monto - total_base_16, 2)

            totales = {
                "TotalTrasladosBaseIVA16": f"{total_base_16:.2f}",
                "TotalTrasladosImpuestoIVA16": f"{total_imp_16:.2f}",
                "MontoTotalPagos": f"{monto:.2f}"
            }
            impuestos_dr = {
                "TrasladosDR": [
                    {
                        "BaseDR": f"{base_dr:.6f}",
                        "ImpuestoDR": "002",
                        "TipoFactorDR": "Tasa",
                        "TasaOCuotaDR": "0.160000",
                        "ImporteDR": f"{importe_dr:.6f}"
                    }
                ]
            }
            impuestos_p = {
                "TrasladosP": [
                    {
                        "BaseP": f"{base_dr:.6f}",
                        "ImpuestoP": "002",
                        "TipoFactorP": "Tasa",
                        "TasaOCuotaP": "0.160000",
                        "ImporteP": f"{importe_dr:.6f}"
                    }
                ]
            }
            objeto_imp_dr = "02"
        else:
            totales = {
                "MontoTotalPagos": f"{monto:.2f}"
            }
            impuestos_dr = None
            impuestos_p = None
            objeto_imp_dr = "01"

        docto_relacionado = {
            "IdDocumento": factura.get("uuid"),
            "Serie": factura.get("serie") or "",
            "Folio": str(factura.get("folio") or ""),
            "MonedaDR": "MXN",
            "EquivalenciaDR": "1",
            "NumParcialidad": str(num_parcialidad),
            "ImpSaldoAnt": f"{imp_saldo_ant:.2f}",
            "ImpPagado": f"{monto:.2f}",
            "ImpSaldoInsoluto": f"{imp_saldo_insoluto:.2f}",
            "ObjetoImpDR": objeto_imp_dr
        }
        if impuestos_dr:
            docto_relacionado["ImpuestosDR"] = impuestos_dr

        pago_item = {
            "FechaPago": fecha_pago,
            "FormaDePagoP": forma_pago,
            "MonedaP": "MXN",
            "TipoCambioP": "1",
            "Monto": f"{monto:.2f}",
            "DoctoRelacionado": [docto_relacionado]
        }
        if num_operacion:
            pago_item["NumOperacion"] = str(num_operacion)
        if impuestos_p:
            pago_item["ImpuestosP"] = impuestos_p

        timbrado = {
            "Version": "4.0",
            "Serie": serie,
            "Folio": secuencia,
            "Fecha": fecha_emision,
            "SubTotal": 0,
            "Moneda": "XXX",
            "Total": 0,
            "TipoDeComprobante": "P",
            "Exportacion": "01",
            "LugarExpedicion": lugar_expedicion,
            "Emisor": {
                "Rfc": emisor_rfc,
                "Nombre": emisor_nombre,
                "RegimenFiscal": emisor_regimen
            },
            "Receptor": {
                "Rfc": receptor_rfc,
                "Nombre": receptor_nombre,
                "DomicilioFiscalReceptor": receptor_domicilio,
                "RegimenFiscalReceptor": receptor_regimen,
                "UsoCFDI": "CP01"
            },
            "Conceptos": [
                {
                    "ClaveProdServ": "84111506",
                    "Cantidad": 1,
                    "ClaveUnidad": "ACT",
                    "Descripcion": "Pago",
                    "ValorUnitario": 0,
                    "Importe": 0,
                    "ObjetoImp": "01"
                }
            ],
            "Complemento": {
                "Any": [
                    {
                        "Pago20:Pagos": {
                            "Version": "2.0",
                            "Totales": totales,
                            "Pago": [pago_item]
                        }
                    }
                ]
            }
        }

        # Timbrar con SW Sapien
        sw_token = get_sw_token()
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
            db["folios"].find_one_and_update(
                {"tipo": "complemento_pago", "sucursal_id": str(sucursal_id)},
                {"$inc": {"secuencia": -1}}
            )
            return create_response(response_sw.status_code, f"Error del PAC: {response_sw.text}")

        factura_generada = response_sw.json()
        if factura_generada.get("status") == 'error':
            db["folios"].find_one_and_update(
                {"tipo": "complemento_pago", "sucursal_id": str(sucursal_id)},
                {"$inc": {"secuencia": -1}}
            )
            return create_response(400, factura_generada.get("message") or "Error al generar complemento de pago")

        # Formatear XML
        cfdi_raw = factura_generada["data"]["cfdi"]
        dom = xml.dom.minidom.parseString(cfdi_raw)
        pretty_xml = dom.toprettyxml(indent="  ", encoding="UTF-8").decode("utf-8")

        # Persistir complemento en facturasemitidas
        complemento_doc = {
            "tipo_de_comprobante": "P",
            "factura_padre_id": str(factura_oid),
            "factura_padre_uuid": factura.get("uuid"),
            "factura_padre_serie": factura.get("serie") or "",
            "factura_padre_folio": factura.get("folio") or "",
            "num_parcialidad": num_parcialidad,
            "imp_saldo_ant": round(imp_saldo_ant, 2),
            "imp_pagado": round(monto, 2),
            "imp_saldo_insoluto": round(imp_saldo_insoluto, 2),
            "fecha_pago": fecha_pago,
            "forma_pago": forma_pago,
            "num_operacion": str(num_operacion or ""),
            "cadenaOriginalSAT": factura_generada["data"].get("cadenaOriginalSAT"),
            "cfdi": pretty_xml,
            "fechaTimbrado": factura_generada["data"].get("fechaTimbrado"),
            "noCertificadoCFDI": factura_generada["data"].get("noCertificadoCFDI"),
            "noCertificadoSAT": factura_generada["data"].get("noCertificadoSAT"),
            "qrCode": factura_generada["data"].get("qrCode"),
            "selloCFDI": factura_generada["data"].get("selloCFDI"),
            "selloSAT": factura_generada["data"].get("selloSAT"),
            "uuid": factura_generada["data"].get("uuid"),
            "sucursal": str(sucursal_id),
            "idCertificado": id_certificado,
            "ticket": factura.get("ticket") or "",
            "serie": serie,
            "folio": secuencia,
            "rfc_receptor": receptor_rfc,
            "nombre_receptor": receptor_nombre,
            "uso_cfdi": "CP01",
            "regimen_fiscal_receptor": receptor_regimen,
            "domicilio_fiscal_receptor": receptor_domicilio,
            "moneda": "XXX",
            "subtotal": 0.0,
            "total": 0.0,
            "estatus": "Vigente",
            "tenant_id": tenant_id,
            "createdAt": datetime.utcnow()
        }
        res_insert = db["facturasemitidas"].insert_one(complemento_doc)
        complemento_id = str(res_insert.inserted_id)

        # Actualizar factura padre
        db["facturasemitidas"].update_one(
            {"_id": factura_oid},
            {"$set": {
                "saldo_insoluto": round(imp_saldo_insoluto, 2),
                "esta_liquidada": (round(imp_saldo_insoluto, 2) <= 0.001),
                "ultima_parcialidad": num_parcialidad
            }}
        )

        # Generar PDF del Complemento
        pdf_b64 = None
        logo_temp_file = None
        try:
            platform_db = get_platform_db()
            taller = platform_db["talleres"].find_one({"tenantId": tenant_id})
            logo_url = taller.get("logoUrl") if taller else None
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
                    logger.warning(f"No se pudo descargar logotipo para el PDF del complemento: {img_err}")

            reg_emisor_doc = db["regimenfiscal"].find_one({"regimenfiscal": emisor_regimen})
            regimen_fiscal_emisor = reg_emisor_doc.get("descripcion") if reg_emisor_doc else emisor_regimen

            reg_receptor_doc = db["regimenfiscal"].find_one({"regimenfiscal": receptor_regimen})
            regimen_fiscal_receptor = reg_receptor_doc.get("descripcion") if reg_receptor_doc else receptor_regimen

            pdf_gen = CFDIPDF_FPDF_Generator(
                xml_string=cfdi_raw,
                qrCode=factura_generada["data"].get("qrCode") or "",
                cadena_original_sat=factura_generada["data"].get("cadenaOriginalSAT") or "",
                noTicket=factura.get("ticket") or "",
                fecha_hora_venta=fecha_pago,
                direccion=sucursal.get("direccion") or "",
                empresa=emisor_nombre or id_certificado,
                regimen_fiscal_emisor=regimen_fiscal_emisor,
                regimen_fiscal_receptor=regimen_fiscal_receptor,
                logo_path=logo_path
            )
            pdf_bytes = pdf_gen.generate_pdf()
            pdf_b64 = base64.b64encode(pdf_bytes).decode('utf-8')
        except Exception as pdf_err:
            logger.exception(f"Error no fatal generando PDF del complemento de pago: {pdf_err}")
        finally:
            if logo_temp_file and os.path.exists(logo_temp_file.name):
                try:
                    os.unlink(logo_temp_file.name)
                except Exception:
                    pass

        res_payload = {
            "id": complemento_id,
            "cfdi": pretty_xml,
            "uuid": factura_generada["data"].get("uuid"),
            "folio": secuencia,
            "serie": serie,
            "num_parcialidad": num_parcialidad,
            "imp_saldo_ant": round(imp_saldo_ant, 2),
            "imp_pagado": round(monto, 2),
            "imp_saldo_insoluto": round(imp_saldo_insoluto, 2),
            "esta_liquidada": (round(imp_saldo_insoluto, 2) <= 0.001),
            "pdf_cfdi_b64": pdf_b64
        }
        return create_response(200, "Complemento de pago generado exitosamente", res_payload)

    except Exception as e:
        logger.exception("Error al timbrar complemento de pago")
        return handle_exception(e)
