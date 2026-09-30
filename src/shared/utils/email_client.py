"""Cliente SMTP para envío de correos transaccionales.

Usa smtplib (stdlib) para no agregar dependencias al layer de Lambda.
Lee credenciales del entorno igual que openpay_client.py.

Uso básico:
    from src.shared.utils.email_client import send_email

    send_email(
        to="cliente@ejemplo.com",
        subject="Su factura de suscripción",
        body_html="<p>Hola <b>Taller XYZ</b>, adjunto tu factura.</p>",
        attachments=[
            {"filename": "factura.pdf", "data": pdf_bytes, "mimetype": "application/pdf"},
            {"filename": "factura.xml", "data": xml_bytes, "mimetype": "text/xml"},
        ],
    )
"""

import os
import smtplib
import ssl
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from aws_lambda_powertools import Logger

logger = Logger()

# ── Configuración desde entorno ───────────────────────────────────────────────
_HOST      = os.environ.get("SMTP_HOST", "").strip()
_PORT      = int(os.environ.get("SMTP_PORT", "587"))
_USER      = os.environ.get("SMTP_USER", "").strip()
_PASSWORD  = os.environ.get("SMTP_PASSWORD", "").strip()
_FROM      = os.environ.get("SMTP_FROM", "").strip()
_FROM_NAME = os.environ.get("SMTP_FROM_NAME", "Mekanics Manager").strip()
_USE_TLS   = os.environ.get("SMTP_USE_TLS", "true").lower() == "true"


def send_email(
    to: "str | list[str]",
    subject: str,
    body_html: Optional[str] = None,
    body_text: Optional[str] = None,
    attachments: Optional[list] = None,
    reply_to: Optional[str] = None,
) -> None:
    """Envía un correo electrónico con adjuntos opcionales.

    Args:
        to: Destinatario(s). Puede ser un string o lista de strings.
        subject: Asunto del correo.
        body_html: Cuerpo en HTML (recomendado).
        body_text: Cuerpo en texto plano (fallback si no hay HTML).
        attachments: Lista de dicts, cada uno con:
            - 'filename': nombre del archivo (ej. "factura.pdf")
            - 'data': bytes del archivo
            - 'mimetype': tipo MIME completo (ej. "application/pdf", "text/xml")
        reply_to: Dirección de respuesta distinta al remitente.

    Raises:
        RuntimeError: Si las credenciales SMTP no están configuradas.
        Exception: Si el envío falla (loguea el error antes de relanzar).
    """
    if not _HOST or not _USER or not _PASSWORD:
        raise RuntimeError(
            "Credenciales SMTP no configuradas. "
            "Verifica SMTP_HOST, SMTP_USER y SMTP_PASSWORD en las variables de entorno."
        )

    recipients = [to] if isinstance(to, str) else list(to)

    # Contenedor principal "mixed" para soportar adjuntos
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"]    = f"{_FROM_NAME} <{_FROM}>" if _FROM_NAME else _FROM
    msg["To"]      = ", ".join(recipients)
    if reply_to:
        msg["Reply-To"] = reply_to

    # Parte alternativa (texto plano + HTML)
    alt = MIMEMultipart("alternative")
    if body_text:
        alt.attach(MIMEText(body_text, "plain", "utf-8"))
    if body_html:
        alt.attach(MIMEText(body_html, "html", "utf-8"))
    msg.attach(alt)

    # Adjuntos
    for att in (attachments or []):
        mime_main, mime_sub = att["mimetype"].split("/", 1)
        part = MIMEBase(mime_main, mime_sub)
        part.set_payload(att["data"])
        encoders.encode_base64(part)
        part.add_header(
            "Content-Disposition",
            "attachment",
            filename=att["filename"],
        )
        msg.attach(part)

    try:
        if _USE_TLS:
            # STARTTLS — port 587 (recomendado)
            with smtplib.SMTP(_HOST, _PORT, timeout=15) as server:
                server.ehlo()
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
                server.login(_USER, _PASSWORD)
                server.sendmail(_FROM, recipients, msg.as_string())
        else:
            # SSL directo — port 465
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(_HOST, _PORT, context=context, timeout=15) as server:
                server.login(_USER, _PASSWORD)
                server.sendmail(_FROM, recipients, msg.as_string())

        logger.info(
            f"Correo enviado exitosamente",
            extra={"to": recipients, "subject": subject},
        )

    except Exception as exc:
        logger.error(
            f"Error al enviar correo",
            extra={"to": recipients, "subject": subject, "error": str(exc)},
        )
        raise
