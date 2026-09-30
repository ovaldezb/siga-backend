"""Worker SQS — Facturación automática de suscripción mensual.

Flujo:
  1. Webhook de Openpay (pagos_manager.openpay_webhook_handler) confirma pago exitoso.
  2. Encola un mensaje en SQS FIFO con los datos del taller y la transacción.
  3. Este worker se dispara automáticamente, timbra el CFDI 4.0 ante el SAT
     via SW Sapien, genera el PDF y envía el correo con PDF + XML al adminEmail del taller.

Garantías:
  - La MessageDeduplicationId (trans_id de Openpay) evita procesar el mismo pago dos veces.
  - Si falla, SQS reintenta hasta maxReceiveCount (3). Luego va a la DLQ.
  - Usa ReportBatchItemFailures para reintentar solo los mensajes fallidos del lote.
  - Solo genera factura si el taller tiene requiereFactura=True y datosFiscales completos.
"""

import base64
import json
import os
import xml.dom.minidom
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from aws_lambda_powertools import Logger
from bson import ObjectId
from pymongo import ReturnDocument

from src.handlers.facturacion.certificates_manager import get_sw_token
from src.handlers.facturacion.cfdi_pdf_fpdf_generator import CFDIPDF_FPDF_Generator
from src.shared.infrastructure.database import get_platform_db
from src.shared.utils.email_client import send_email

logger = Logger()

SW_URL = os.getenv("SW_URL", "")

# Constantes SAT para factura de suscripción de plataforma
# (mismas que usa platform_facturacion_manager)
FACTURA_CLAVE_PROD_SERV = "81112100"
FACTURA_CLAVE_UNIDAD = "MON"
FACTURA_UNIDAD = "Mes"
FACTURA_DESCRIPCION = "Servicio de hospedaje de aplicación MekanicsManager"
FACTURA_OBJETO_IMP = "02"


@logger.inject_lambda_context
def handler(event, context):
    """Punto de entrada SQS.
    Configurado con batchSize=1 y ReportBatchItemFailures para control de errores preciso.
    """
    failed_items = []
    for record in event.get("Records", []):
        msg_id = record["messageId"]
        try:
            body = json.loads(record["body"])
            logger.info(
                "Procesando mensaje de facturación",
                extra={"msg_id": msg_id, "tenant_id": body.get("tenant_id")},
            )
            _procesar_facturacion(body)
        except Exception as exc:
            logger.error(
                f"Error procesando mensaje SQS {msg_id}: {exc}",
                exc_info=True,
            )
            failed_items.append({"itemIdentifier": msg_id})

    # SQS reintentará automáticamente los mensajes en failed_items
    return {"batchItemFailures": failed_items}


def _limpiar_razon_social(nombre: str) -> str:
    """Elimina el tipo societario del nombre para cumplir CFDI40139/CFDI40145."""
    import re
    if not nombre:
        return ""
    nombre = nombre.strip().upper()
    patron = (
        r"(?:,\s*|\s+)"
        r"(S\.?\s*A\.?\s*P\.?\s*I\.?\s*(?:DE\s+C\.?\s*V\.?)?|"
        r"S\.?\s*A\.?\s*DE\s+C\.?\s*V\.?|"
        r"S\.?\s*DE\s+R\.?\s*L\.?\s*(?:DE\s+C\.?\s*V\.?)?|"
        r"S\.?\s*A\.?|S\.?\s*C\.?|S\.?\s*N\.?\s*C\.?|"
        r"S\.?\s*C\.?\s*S\.?|A\.?\s*C\.?|S\.?\s*A\.?\s*S\.?)\\.?$"
    )
    return re.sub(patron, "", nombre, flags=re.IGNORECASE).strip()


def _procesar_facturacion(body: dict) -> None:
    """Timbra la factura de suscripción y envía el correo al taller.

    Args:
        body: Payload del mensaje SQS. Debe contener:
            - tenant_id: tenantId del taller
            - pago_id: str(_id) del documento en suscripciones_pagos
            - trans_id: ID de la transacción de Openpay (para logs)
            - admin_email: correo del administrador del taller
            - nombre_taller: nombre comercial del taller
            - monto: monto total del pago
            - metodo: método de pago (para mapear forma_pago SAT)
    """
    tenant_id   = body["tenant_id"]
    pago_id     = body["pago_id"]
    trans_id    = body.get("trans_id", "")
    admin_email = body["admin_email"]
    nombre_taller = body.get("nombre_taller", "")
    monto       = float(body["monto"])
    metodo      = body.get("metodo", "")

    db = get_platform_db()

    # ── 1. Verificar que el pago no haya sido ya facturado (idempotencia) ────
    pago = db["suscripciones_pagos"].find_one({"_id": ObjectId(pago_id)})
    if not pago:
        raise ValueError(f"Pago {pago_id} no encontrado en suscripciones_pagos")

    if pago.get("facturado"):
        logger.info(
            f"Pago {pago_id} ya fue facturado (UUID: {pago.get('uuid')}). Ignorando."
        )
        return  # Éxito — el mensaje se elimina de la cola sin reintentar

    # ── 2. Obtener taller y validar requiereFactura ───────────────────────────
    taller = db["talleres"].find_one({"tenantId": tenant_id})
    if not taller:
        raise ValueError(f"Taller con tenantId={tenant_id} no encontrado")

    if not taller.get("requiereFactura", False):
        logger.info(
            f"Taller {tenant_id} no requiere factura (requiereFactura=False). "
            "Omitiendo timbrado."
        )
        return  # Sin factura — el mensaje se elimina limpiamente

    # ── 3. Validar datos fiscales del taller (receptor) ──────────────────────
    datos_fiscales = taller.get("datosFiscales") or {}
    rfc_receptor     = (datos_fiscales.get("rfc") or "").strip().upper()
    nombre_receptor  = _limpiar_razon_social(
        datos_fiscales.get("razonSocial") or taller.get("nombreComercial") or ""
    )
    cp_receptor      = (datos_fiscales.get("codigoPostal") or "").strip()
    regimen_receptor = (datos_fiscales.get("regimenFiscal") or "").strip()
    uso_cfdi         = (datos_fiscales.get("usoCfdi") or "G03").strip().upper()

    if not rfc_receptor or not cp_receptor or not regimen_receptor:
        raise ValueError(
            f"Taller {tenant_id} tiene requiereFactura=True pero le faltan datos fiscales "
            f"(rfc={rfc_receptor!r}, cp={cp_receptor!r}, regimen={regimen_receptor!r}). "
            "Corregir en el panel de administración."
        )

    # ── 4. Obtener configuración del emisor (plataforma) ─────────────────────
    sucursal = db["sucursales"].find_one({"is_platform": True}) or db["sucursales"].find_one() or {}

    emisor_rfc     = (sucursal.get("rfc") or "").strip().upper()
    emisor_nombre  = _limpiar_razon_social(sucursal.get("nombre") or "MEKANICS MANAGER")
    emisor_regimen = (sucursal.get("regimen_fiscal") or "").strip()
    lugar_exp      = (sucursal.get("codigo_postal") or "").strip()
    serie          = sucursal.get("serie", "F")
    id_certificado = sucursal.get("id_certificado")

    if not emisor_rfc or not emisor_regimen or not lugar_exp:
        raise ValueError(
            "Configuración fiscal del emisor (plataforma) incompleta. "
            "Completar en 'Clientes Facturas → Configuración Emisor'."
        )

    # ── 5. Verificar CSD ─────────────────────────────────────────────────────
    cert_doc = None
    if id_certificado:
        try:
            cert_doc = db["certificates"].find_one({"_id": ObjectId(id_certificado)})
        except Exception:
            pass
    if not cert_doc:
        cert_doc = db["certificates"].find_one()
    if not cert_doc:
        raise ValueError("No hay CSD (Certificado de Sello Digital) configurado en la plataforma.")

    # ── 6. Incrementar folio ─────────────────────────────────────────────────
    folio_doc = db["folios"].find_one_and_update(
        {"tipo": "factura_platform"},
        {"$inc": {"secuencia": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    secuencia = folio_doc.get("secuencia", 1)

    # ── 7. Calcular montos (IVA 16% incluido) ────────────────────────────────
    total_pago   = round(monto, 2)
    subtotal_val = round(total_pago / 1.16, 2)
    iva_val      = round(total_pago - subtotal_val, 2)

    metodo_upper = str(metodo).upper()
    if "CARD" in metodo_upper or "TARJETA" in metodo_upper:
        forma_pago_sat = "04"
    elif "SPEI" in metodo_upper or "TRANSFER" in metodo_upper or "BANK" in metodo_upper:
        forma_pago_sat = "03"
    else:
        forma_pago_sat = "04"

    try:
        now_cdmx = datetime.now(ZoneInfo("America/Mexico_City"))
    except Exception:
        now_cdmx = datetime.utcnow()
    fecha_emision = now_cdmx.strftime("%Y-%m-%dT%H:%M:%S")

    # ── 8. Construir payload CFDI 4.0 ────────────────────────────────────────
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
        "LugarExpedicion": lugar_exp,
        "Emisor": {
            "Rfc": emisor_rfc,
            "Nombre": emisor_nombre,
            "RegimenFiscal": emisor_regimen,
        },
        "Receptor": {
            "Rfc": rfc_receptor,
            "Nombre": nombre_receptor,
            "DomicilioFiscalReceptor": cp_receptor,
            "RegimenFiscalReceptor": regimen_receptor,
            "UsoCFDI": uso_cfdi,
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
                            "Importe": iva_val,
                        }
                    ]
                },
            }
        ],
        "Impuestos": {
            "Traslados": [
                {
                    "Base": subtotal_val,
                    "Impuesto": "002",
                    "TipoFactor": "Tasa",
                    "TasaOCuota": "0.160000",
                    "Importe": iva_val,
                }
            ],
            "TotalImpuestosTrasladados": iva_val,
        },
    }

    # ── 9. Timbrar ante el SAT via SW Sapien ─────────────────────────────────
    sw_token = get_sw_token()
    res_sw = requests.post(
        f"{SW_URL}/v3/cfdi33/issue/json/v4",
        headers={
            "Content-Type": "application/jsontoxml",
            "Authorization": f"Bearer {sw_token}",
        },
        data=json.dumps(timbrado_payload),
        timeout=30,
    )

    if res_sw.status_code != 200:
        # Revertir folio para evitar huecos en la secuencia
        db["folios"].find_one_and_update(
            {"tipo": "factura_platform"}, {"$inc": {"secuencia": -1}}
        )
        raise RuntimeError(
            f"Error HTTP de SW Sapien al timbrar: {res_sw.status_code} — {res_sw.text[:300]}"
        )

    factura_res = res_sw.json()
    if factura_res.get("status") == "error":
        db["folios"].find_one_and_update(
            {"tipo": "factura_platform"}, {"$inc": {"secuencia": -1}}
        )
        raise RuntimeError(
            f"Error del SAT al timbrar: {factura_res.get('message')} — "
            f"{factura_res.get('messageDetail', '')}"
        )

    # ── 10. Formatear XML ────────────────────────────────────────────────────
    cfdi_raw  = factura_res["data"]["cfdi"]
    dom       = xml.dom.minidom.parseString(cfdi_raw)
    pretty_xml = dom.toprettyxml(indent="  ", encoding="UTF-8").decode("utf-8")
    uuid       = factura_res["data"].get("uuid", "")

    # ── 11. Persistir factura en _platform['facturasemitidas'] ───────────────
    factura_doc = {
        "cadenaOriginalSAT": factura_res["data"].get("cadenaOriginalSAT"),
        "cfdi": pretty_xml,
        "fechaTimbrado": factura_res["data"].get("fechaTimbrado"),
        "noCertificadoCFDI": factura_res["data"].get("noCertificadoCFDI"),
        "noCertificadoSAT": factura_res["data"].get("noCertificadoSAT"),
        "qrCode": factura_res["data"].get("qrCode"),
        "selloCFDI": factura_res["data"].get("selloCFDI"),
        "selloSAT": factura_res["data"].get("selloSAT"),
        "uuid": uuid,
        "pagoId": pago_id,
        "tallerTenantId": tenant_id,
        "tallerNombre": nombre_taller,
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
        "origen": "automatico_suscripcion",
        "createdAt": datetime.utcnow(),
    }
    res_insert = db["facturasemitidas"].insert_one(factura_doc)
    factura_id = str(res_insert.inserted_id)

    # ── 12. Marcar pago como facturado ───────────────────────────────────────
    db["suscripciones_pagos"].update_one(
        {"_id": ObjectId(pago_id)},
        {
            "$set": {
                "facturado": True,
                "facturaId": factura_id,
                "uuid": uuid,
                "fechaFacturacion": datetime.utcnow(),
            }
        },
    )

    # ── 13. Generar PDF ──────────────────────────────────────────────────────
    # Convertir fechaPago a string ISO — MongoDB lo devuelve como datetime.datetime
    # pero CFDIPDF_FPDF_Generator espera un string (hace 'if T in fecha_clean').
    fecha_pago_raw = pago.get("fechaPago")
    if isinstance(fecha_pago_raw, datetime):
        fecha_hora_venta_str = fecha_pago_raw.strftime("%Y-%m-%dT%H:%M:%S")
    elif fecha_pago_raw:
        fecha_hora_venta_str = str(fecha_pago_raw)
    else:
        fecha_hora_venta_str = fecha_emision  # fallback: fecha de timbrado

    try:
        pdf_gen = CFDIPDF_FPDF_Generator(
            xml_string=pretty_xml,
            qrCode=factura_res["data"].get("qrCode") or "",
            cadena_original_sat=factura_res["data"].get("cadenaOriginalSAT") or "",
            noTicket=f"PAGO-{pago_id[:8]}",
            fecha_hora_venta=fecha_hora_venta_str,
            direccion=sucursal.get("direccion", ""),
            empresa=emisor_nombre,
            regimen_fiscal_emisor=emisor_regimen,
            regimen_fiscal_receptor=regimen_receptor,
            mostrar_observaciones=False,
        )
        pdf_bytes = pdf_gen.generate_pdf()
    except Exception as pdf_err:
        logger.error(f"Error generando PDF para factura {uuid}: {pdf_err}")
        raise  # Relanzar — SQS reintentará; la factura ya está en Mongo pero el correo aún no se envió


    # ── 14. Enviar correo con PDF y XML adjuntos ──────────────────────────────
    xml_bytes     = pretty_xml.encode("utf-8")
    nombre_archivo = f"Factura-{uuid[:8]}"

    html_body = _build_email_html(
        nombre_taller=nombre_taller,
        uuid=uuid,
        folio=f"{serie}{secuencia}",
        total=total_pago,
        fecha=fecha_emision[:10],
    )

    send_email(
        to=admin_email,
        subject=f"Tu factura de suscripción — {nombre_taller} — {fecha_emision[:10]}",
        body_html=html_body,
        attachments=[
            {
                "filename": f"{nombre_archivo}.pdf",
                "data": pdf_bytes,
                "mimetype": "application/pdf",
            },
            {
                "filename": f"{nombre_archivo}.xml",
                "data": xml_bytes,
                "mimetype": "text/xml",
            },
        ],
    )

    logger.info(
        "Facturación automática completada exitosamente",
        extra={
            "uuid": uuid,
            "factura_id": factura_id,
            "tenant_id": tenant_id,
            "admin_email": admin_email,
            "trans_id": trans_id,
        },
    )


def _build_email_html(
    nombre_taller: str,
    uuid: str,
    folio: str,
    total: float,
    fecha: str,
) -> str:
    """Genera el cuerpo HTML del correo de factura de suscripción."""
    return f"""<!DOCTYPE html>
<html lang="es">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f4f4f4;font-family:Arial,Helvetica,sans-serif">
  <table width="100%" cellpadding="0" cellspacing="0">
    <tr>
      <td align="center" style="padding:30px 20px">
        <table width="600" cellpadding="0" cellspacing="0"
               style="background:#ffffff;border-radius:8px;overflow:hidden;
                      box-shadow:0 2px 8px rgba(0,0,0,.08)">

          <!-- Cabecera -->
          <tr>
            <td style="background:#1a73e8;padding:24px 32px">
              <h1 style="margin:0;color:#ffffff;font-size:22px;font-weight:700">
                Mekanics Manager
              </h1>
              <p style="margin:4px 0 0;color:#d2e3fc;font-size:13px">
                Gestión de talleres automotrices
              </p>
            </td>
          </tr>

          <!-- Cuerpo -->
          <tr>
            <td style="padding:32px">
              <p style="margin:0 0 16px;font-size:16px;color:#333">
                Hola <strong>{nombre_taller}</strong>,
              </p>
              <p style="margin:0 0 24px;font-size:14px;color:#555;line-height:1.6">
                Tu pago de suscripción mensual fue procesado y tu factura fiscal
                ha sido timbrada ante el SAT. La encontrarás adjunta en formato
                <strong>PDF</strong> y <strong>XML</strong>.
              </p>

              <!-- Tabla de detalles -->
              <table width="100%" cellpadding="0" cellspacing="0"
                     style="border-collapse:collapse;font-size:14px;margin-bottom:24px">
                <tr style="background:#f8f9fa">
                  <td style="padding:10px 14px;border:1px solid #e0e0e0;color:#666;width:40%">
                    <strong>Folio</strong>
                  </td>
                  <td style="padding:10px 14px;border:1px solid #e0e0e0;color:#333">
                    {folio}
                  </td>
                </tr>
                <tr>
                  <td style="padding:10px 14px;border:1px solid #e0e0e0;color:#666">
                    <strong>UUID Fiscal</strong>
                  </td>
                  <td style="padding:10px 14px;border:1px solid #e0e0e0;color:#333;font-size:12px">
                    {uuid}
                  </td>
                </tr>
                <tr style="background:#f8f9fa">
                  <td style="padding:10px 14px;border:1px solid #e0e0e0;color:#666">
                    <strong>Fecha de emisión</strong>
                  </td>
                  <td style="padding:10px 14px;border:1px solid #e0e0e0;color:#333">
                    {fecha}
                  </td>
                </tr>
                <tr>
                  <td style="padding:10px 14px;border:1px solid #e0e0e0;color:#666">
                    <strong>Total</strong>
                  </td>
                  <td style="padding:10px 14px;border:1px solid #e0e0e0;
                             color:#1a73e8;font-weight:700;font-size:16px">
                    ${total:,.2f} MXN
                  </td>
                </tr>
              </table>

              <p style="margin:0;font-size:13px;color:#888;line-height:1.6">
                Si tienes alguna pregunta sobre tu factura, contáctanos en
                <a href="mailto:soporte@mekanicsmanager.com"
                   style="color:#1a73e8">soporte@mekanicsmanager.com</a>.
              </p>
            </td>
          </tr>

          <!-- Pie -->
          <tr>
            <td style="background:#f8f9fa;padding:16px 32px;border-top:1px solid #e0e0e0">
              <p style="margin:0;font-size:11px;color:#aaa;text-align:center">
                © {fecha[:4]} Mekanics Manager &mdash; Este es un correo automático, no responder.
              </p>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""
