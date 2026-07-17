import json
import base64
import os
import re
from http import HTTPStatus
from datetime import datetime
import requests
from requests_toolbelt.multipart import decoder
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from bson import ObjectId

from aws_lambda_powertools import Logger
from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.infrastructure.database import get_tenant_db
from src.shared.utils.auth_utils import get_claims
from src.shared.utils.date_utils import iso_utc

logger = Logger()

SW_USER_NAME = os.getenv("SW_USER_NAME")
SW_USER_PASSWORD = os.getenv("SW_USER_PASSWORD")
SW_URL = os.getenv("SW_URL")

def get_sw_token():
    sw_token_req = requests.post(
        f"{SW_URL}/v2/security/authenticate",
        headers={"Content-Type": "application/json"},
        data=json.dumps({"user": SW_USER_NAME, "password": SW_USER_PASSWORD})
    )
    if sw_token_req.status_code != 200:
        raise Exception("No se pudo autenticar con SW Sapien")
    return sw_token_req.json().get('data', {}).get('token')

@logger.inject_lambda_context
def create_certificate_handler(event, context):
    try:
        claims = get_claims(event)
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        db = get_tenant_db(tenant_id)
        
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
            return create_response(400, "Faltan parámetros obligatorios (cer, key, ctrsn)")

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
            "tenant_id": tenant_id,
            "createdAt": datetime.utcnow()
        }

        result = db["certificates"].insert_one(certificado_doc)
        certificado_doc["_id"] = str(result.inserted_id)
        certificado_doc["desde"] = iso_utc(certificado_doc["desde"])
        certificado_doc["hasta"] = iso_utc(certificado_doc["hasta"])
        certificado_doc["createdAt"] = iso_utc(certificado_doc["createdAt"])
        
        return create_response(201, "Certificado guardado exitosamente", certificado_doc)

    except Exception as e:
        return handle_exception(e)

@logger.inject_lambda_context
def get_certificates_handler(event, context):
    try:
        claims = get_claims(event)
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        db = get_tenant_db(tenant_id)
        certificates = list(db["certificates"].find({"tenant_id": tenant_id}))
        
        for cert in certificates:
            cert["_id"] = str(cert["_id"])
            if "desde" in cert and isinstance(cert["desde"], datetime):
                cert["desde"] = iso_utc(cert["desde"])
            if "hasta" in cert and isinstance(cert["hasta"], datetime):
                cert["hasta"] = iso_utc(cert["hasta"])
            if "createdAt" in cert and isinstance(cert["createdAt"], datetime):
                cert["createdAt"] = iso_utc(cert["createdAt"])

        return create_response(200, "Certificados listados", certificates)
    except Exception as e:
        return handle_exception(e)

@logger.inject_lambda_context
def delete_certificate_handler(event, context):
    try:
        claims = get_claims(event)
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        cert_id = event['pathParameters']['id']
        db = get_tenant_db(tenant_id)
        
        certificate = db["certificates"].find_one({"_id": ObjectId(cert_id), "tenant_id": tenant_id})
        if not certificate:
            return create_response(404, "Certificado no encontrado")
            
        token = get_sw_token()
        requests.delete(
            f"{SW_URL}/certificates/" + certificate["no_certificado"],
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}"
            }
        ).json()
        
        db["certificates"].delete_one({"_id": ObjectId(cert_id)})
        # Limpiar referencias de las sucursales
        db["sucursales"].update_many(
            {"id_certificado": cert_id},
            {"$unset": {"id_certificado": ""}}
        )

        return create_response(200, "Certificado eliminado")
    except Exception as e:
        return handle_exception(e)

@logger.inject_lambda_context
def update_certificate_handler(event, context):
    try:
        claims = get_claims(event)
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        db = get_tenant_db(tenant_id)
        cert_id = event['pathParameters']['id']
        
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
            return create_response(400, "Faltan parámetros obligatorios (cer, key, ctrsn)")

        certificado_actual = db["certificates"].find_one({"_id": ObjectId(cert_id), "tenant_id": tenant_id})
        if not certificado_actual:
            return create_response(404, "Certificado no encontrado")

        cert_x509 = x509.load_der_x509_certificate(cer_bytes, default_backend())
        subject = cert_x509.subject.rfc4514_string()
        rfc_match = re.search(r'2\.5\.4\.45=([A-Z0-9]+)', subject)
        rfc_nuevo = rfc_match.group(1) if rfc_match else None
        
        if certificado_actual.get("rfc") != rfc_nuevo:
            return create_response(400, "El RFC del certificado no coincide con el inicial")

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

        return create_response(200, "Certificado actualizado correctamente")
    except Exception as e:
        return handle_exception(e)
