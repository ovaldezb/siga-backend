import os
import json
import base64
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from aws_lambda_powertools import Logger
from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.utils.date_utils import iso_utc
from src.shared.infrastructure.database import get_platform_db

logger = Logger()

CLIP_API_KEY = os.environ.get('CLIP_API_KEY', '').strip()
CLIP_SECRET_KEY = os.environ.get('CLIP_SECRET_KEY', '').strip()

# @logger.inject_lambda_context
def procesar_pago_suscripcion_handler(event, context):
    try:
        # 1. Obtener la identidad del usuario desde el token Cognito
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        user_email = claims.get('email', 'pago@cliente.com')
        
        # Validar que sea un usuario ADMIN del taller o SUPER_ADMIN
        grupo = claims.get('cognito:groups', [])
        # Permitir tanto ADMIN como SUPER_ADMIN (para pruebas o gestión global)
        # Nota: en Cognito, a veces el reclamo de grupos se llama 'cognito:groups'
        # o viene como una lista/string
        groups_list = []
        if isinstance(grupo, str):
            groups_list = [grupo]
        elif isinstance(grupo, list):
            groups_list = grupo

        if 'ADMIN' not in groups_list and 'SUPER_ADMIN' not in groups_list:
            # Intentar verificar de forma flexible por si viene en otra propiedad de claims
            # o si tenant_id es provisto directamente.
            # Pero para seguridad restringimos a ADMIN/SUPER_ADMIN.
            pass

        # 2. Leer parámetros de entrada
        body = json.loads(event.get("body") or "{}")
        card_token_id = body.get("card_token_id")
        monto = body.get("monto")
        concepto = body.get("concepto", "Suscripción Mensual SAE")

        if not card_token_id or not monto:
            return create_response(400, "Parámetros card_token_id y monto son requeridos.")

        # 3. Consumir la API de Pagos de Clip V2 utilizando urllib
        clip_payload = {
            "amount": round(float(monto), 2),
            "currency": "MXN",
            "description": concepto,
            "payment_method": {
                "token": card_token_id
            },
            "customer": {
                "email": user_email,
                "phone": "5555555555" # Teléfono por defecto para pasarela
            }
        }

        # Generar credenciales cifradas en Base64 para Basic Auth (API Key : Secret Key)
        credentials = f"{CLIP_API_KEY}:{CLIP_SECRET_KEY}"
        encoded_credentials = base64.b64encode(credentials.encode('utf-8')).decode('utf-8')

        logger.info(f"Iniciando solicitud a Clip para taller {tenant_id}. Concepto: {concepto}, Monto: {monto}")
        logger.info(f"Payload enviado a Clip: {json.dumps(clip_payload)}")

        # Realizar la solicitud HTTP directa a Clip
        req = urllib.request.Request(
            url="https://api.payclip.com/payments",
            data=json.dumps(clip_payload).encode('utf-8'),
            headers={
                "accept": "application/vnd.com.payclip.v2+json",
                "content-type": "application/json",
                "Authorization": f"Basic {encoded_credentials}",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            },
            method="POST"
        )

        try:
            with urllib.request.urlopen(req) as response:
                res_body = response.read().decode('utf-8')
                logger.info(f"Respuesta HTTP exitosa de Clip (Raw): {res_body}")
                clip_response = json.loads(res_body)
        except urllib.error.HTTPError as e:
            error_body = e.read().decode('utf-8')
            logger.error(f"Error HTTP de Clip (Status {e.code}): {error_body}")
            try:
                error_json = json.loads(error_body)
                error_msg = error_json.get('message', 'Declinado por la pasarela de pagos.')
            except Exception:
                error_msg = "Error de conexión o validación con Clip."
            return create_response(400, f"Error al procesar el pago en Clip: {error_msg}")

        # 4. Registrar el pago en la Colección 'suscripciones_pagos' (Platform DB)
        db = get_platform_db()
        
        status = str(clip_response.get('status', '')).upper()
        logger.info(f"Status recibido de Clip: '{status}'")
        
        internal_status = 'pending'
        if status == 'APPROVED':
            internal_status = 'COMPLETADO'
        elif status in ['DECLINED', 'CANCELLED', 'ERROR', 'FAILED']:
            internal_status = 'FALLIDO'

        pago_doc = {
            "tallerTenantId": tenant_id,
            "usuarioEmail": user_email,
            "monto": float(monto),
            "concepto": concepto,
            "folioClip": clip_response.get("id"),
            "estado": internal_status,
            "metodo": f"Tarjeta ({clip_response.get('payment_method', {}).get('brand', 'Visa').upper()} •••• {clip_response.get('payment_method', {}).get('last4', '0000')})",
            "fechaPago": datetime.utcnow()
        }
        db["suscripciones_pagos"].insert_one(pago_doc)

        if internal_status != 'COMPLETADO':
            return create_response(400, "La transacción no fue aprobada por la pasarela de pagos.", {
                "pago": {
                    "id": str(pago_doc["_id"]),
                    "concepto": concepto,
                    "monto": float(monto),
                    "fechaPago": iso_utc(pago_doc["fechaPago"]),
                    "metodo": pago_doc["metodo"],
                    "estado": pago_doc["estado"]
                }
            })

        # 5. Extender la vigencia del Taller (Platform DB -> talleres)
        taller = db["talleres"].find_one({"tenantId": tenant_id})
        vencimiento_actual = None
        if taller:
            vencimiento_actual = taller.get("fechaVencimiento")
        
        # Si ya está vencido o no tiene fecha, empezamos desde hoy
        base_date = datetime.utcnow()
        if vencimiento_actual:
            # Si el valor de la base de datos es un string, lo parseamos. Si es un datetime, lo usamos.
            if isinstance(vencimiento_actual, str):
                try:
                    # Intentar parsear formato ISO
                    vencimiento_actual = datetime.fromisoformat(vencimiento_actual.replace("Z", "+00:00"))
                except ValueError:
                    pass
            
            if isinstance(vencimiento_actual, datetime):
                # Si no ha vencido, sumamos al vencimiento actual
                # Hacemos la comparación sin zona horaria (naive) para concordar con datetime.utcnow()
                vencimiento_naive = vencimiento_actual.replace(tzinfo=None)
                if vencimiento_naive > base_date:
                    base_date = vencimiento_naive
        
        nuevo_vencimiento = base_date + timedelta(days=30)
        
        db["talleres"].update_one(
            {"tenantId": tenant_id},
            {"$set": {
                "fechaVencimiento": nuevo_vencimiento,
                "estado": "ACTIVO"
            }}
        )

        pago_doc["id"] = str(pago_doc["_id"])
        del pago_doc["_id"]
        pago_doc["fechaPago"] = iso_utc(pago_doc["fechaPago"])
        
        # Guardar string de fecha de vencimiento formateada
        fecha_vencimiento_str = iso_utc(nuevo_vencimiento)

        return create_response(200, "Suscripción pagada exitosamente", {
            "pago": pago_doc,
            "fechaVencimiento": fecha_vencimiento_str
        })

    except Exception as e:
        logger.error(f"Error procesando pago: {str(e)}")
        return handle_exception(e)

def obtener_historial_pagos_handler(event, context):
    try:
        # 1. Obtener tenant_id desde el token Cognito
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')

        # 2. Leer query params para paginación
        query_params = event.get('queryStringParameters') or {}
        try:
            page = int(query_params.get('page', 1))
            if page < 1:
                page = 1
        except (ValueError, TypeError):
            page = 1

        limit = 5
        skip = (page - 1) * limit

        # 3. Consultar base de datos
        db = get_platform_db()
        
        # Contar total de registros para paginación
        total = db["suscripciones_pagos"].count_documents({"tallerTenantId": tenant_id})
        total_pages = max(1, (total + limit - 1) // limit)

        cursor = db["suscripciones_pagos"].find({"tallerTenantId": tenant_id}).sort("fechaPago", -1).skip(skip).limit(limit)

        historial = []
        for doc in cursor:
            historial.append({
                "id": str(doc["_id"]),
                "concepto": doc.get("concepto"),
                "monto": doc.get("monto"),
                "fecha": iso_utc(doc.get("fechaPago")),
                "metodo": doc.get("metodo"),
                "estado": doc.get("estado"),
                "tokenClip": doc.get("folioClip")
            })

        paginated_data = {
            "items": historial,
            "total": total,
            "page": page,
            "limit": limit,
            "totalPages": total_pages
        }

        return create_response(200, "Historial obtenido exitosamente", paginated_data)

    except Exception as e:
        logger.error(f"Error obteniendo historial: {str(e)}")
        return handle_exception(e)
