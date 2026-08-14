import os
import json
import base64
import urllib.request
import urllib.error
from aws_lambda_powertools import Logger

logger = Logger()

OPENPAY_MERCHANT_ID = os.environ.get('OPENPAY_MERCHANT_ID', '').strip()
OPENPAY_PRIVATE_KEY = os.environ.get('OPENPAY_PRIVATE_KEY', '').strip()
OPENPAY_PRODUCTION_MODE = os.environ.get('OPENPAY_PRODUCTION_MODE', 'false').lower() == 'true'

def _get_headers():
    # In Openpay, private key is passed as username, password is empty
    credentials = f"{OPENPAY_PRIVATE_KEY}:"
    encoded_credentials = base64.b64encode(credentials.encode('utf-8')).decode('utf-8')
    return {
        "accept": "application/json",
        "content-type": "application/json",
        "Authorization": f"Basic {encoded_credentials}",
        "User-Agent": "MekanicsManager/1.0"
    }

def _get_base_url():
    subdomain = "api" if OPENPAY_PRODUCTION_MODE else "sandbox-api"
    return f"https://{subdomain}.openpay.mx/v1/{OPENPAY_MERCHANT_ID}"

def _request(endpoint: str, data: dict = None, method: str = "POST") -> dict:
    url = f"{_get_base_url()}{endpoint}"
    headers = _get_headers()
    
    payload_bytes = None
    if data is not None:
        payload_bytes = json.dumps(data).encode('utf-8')
        
    req = urllib.request.Request(
        url=url,
        data=payload_bytes,
        headers=headers,
        method=method
    )
    
    try:
        with urllib.request.urlopen(req) as response:
            res_body = response.read().decode('utf-8')
            return json.loads(res_body)
    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8')
        logger.error(f"Openpay HTTP Error (Status {e.code}) on {url}: {error_body}")
        try:
            error_json = json.loads(error_body)
            error_msg = error_json.get('description', 'Error en la API de Openpay.')
            error_code = error_json.get('error_code', 0)
            raise Exception(f"{error_msg} (Código Openpay: {error_code})")
        except json.JSONDecodeError:
            raise Exception(f"Error de comunicación con Openpay: {error_body}")
    except Exception as e:
        logger.error(f"Openpay Request exception on {url}: {str(e)}")
        raise Exception(f"No se pudo establecer conexión con Openpay: {str(e)}")

def create_customer(name: str, last_name: str, email: str) -> dict:
    """Crea un cliente en Openpay."""
    payload = {
        "name": name,
        "last_name": last_name,
        "email": email,
        "requires_account": False
    }
    return _request("/customers", payload, "POST")

def create_spei_charge(customer_id: str, amount: float, description: str, order_id: str) -> dict:
    """Crea un cargo de tipo transferencia bancaria (SPEI) para un cliente."""
    payload = {
        "method": "bank_account",
        "amount": round(float(amount), 2),
        "description": description,
        "order_id": order_id
    }
    return _request(f"/customers/{customer_id}/charges", payload, "POST")

def create_card_charge(customer_id: str, amount: float, description: str, token_id: str, device_session_id: str, order_id: str) -> dict:
    """Crea un cargo inmediato de tipo tarjeta de crédito/débito para un cliente."""
    payload = {
        "method": "card",
        "source_id": token_id,
        "device_session_id": device_session_id,
        "amount": round(float(amount), 2),
        "description": description,
        "order_id": order_id
    }
    return _request(f"/customers/{customer_id}/charges", payload, "POST")
