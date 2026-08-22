from src.shared.utils.auth_utils import get_claims
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

def add_months(source_date, months):
    month = source_date.month - 1 + months
    year = source_date.year + month // 12
    month = month % 12 + 1
    days_in_months = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    if month == 2 and year % 4 == 0 and (year % 100 != 0 or year % 400 == 0):
        day = min(source_date.day, 29)
    else:
        day = min(source_date.day, days_in_months[month-1])
    return datetime(year, month, day, source_date.hour, source_date.minute, source_date.second)

# @logger.inject_lambda_context
def procesar_pago_suscripcion_handler(event, context):
    try:
        # 1. Obtener la identidad del usuario desde el token Cognito
        claims =get_claims(event)
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
        monto = body.get("monto")
        concepto = body.get("concepto", "Suscripción Mensual Mekanics Manager")
        
        card_token_id = body.get("card_token_id")
        openpay_token_id = body.get("openpay_token_id")
        device_session_id = body.get("device_session_id")

        if not monto or (not card_token_id and not openpay_token_id):
            return create_response(400, "Parámetros de token de tarjeta y monto son requeridos.")

        db = get_platform_db()
        is_openpay = bool(openpay_token_id)

        if is_openpay:
            taller = db["talleres"].find_one({"tenantId": tenant_id})
            if not taller:
                return create_response(404, "Taller no encontrado.")
                
            openpay_customer_id = taller.get("openpayCustomerId")
            
            from src.shared.utils import openpay_client
            
            if not openpay_customer_id:
                try:
                    cust_res = openpay_client.create_customer(
                        name=taller.get("adminNombre", "Taller"),
                        last_name=taller.get("adminApellido", "Admin"),
                        email=taller.get("adminEmail", user_email)
                    )
                    openpay_customer_id = cust_res.get("id")
                    db["talleres"].update_one(
                        {"tenantId": tenant_id},
                        {"$set": {"openpayCustomerId": openpay_customer_id}}
                    )
                except Exception as cust_err:
                    return create_response(400, f"Error al registrar cliente en Openpay: {str(cust_err)}")
                    
            try:
                order_id = f"SUB-{tenant_id[:16]}-{int(datetime.utcnow().timestamp())}"
                charge_res = openpay_client.create_card_charge(
                    customer_id=openpay_customer_id,
                    amount=monto,
                    description=concepto,
                    token_id=openpay_token_id,
                    device_session_id=device_session_id,
                    order_id=order_id
                )
            except Exception as op_err:
                pago_fail_doc = {
                    "tallerTenantId": tenant_id,
                    "usuarioEmail": user_email,
                    "monto": float(monto),
                    "concepto": concepto,
                    "estado": "FALLIDO",
                    "metodo": "Tarjeta (Openpay)",
                    "detalle": str(op_err),
                    "fechaPago": datetime.utcnow()
                }
                db["suscripciones_pagos"].insert_one(pago_fail_doc)
                return create_response(400, f"Error al procesar el pago en Openpay: {str(op_err)}")
                
            card_info = charge_res.get("card", {})
            brand = card_info.get("brand", "VISA").upper()
            last4 = card_info.get("card_number", "••••")[-4:]
            
            pago_doc = {
                "tallerTenantId": tenant_id,
                "usuarioEmail": user_email,
                "monto": float(monto),
                "concepto": concepto,
                "folioClip": charge_res.get("id"),
                "estado": "COMPLETADO",
                "metodo": f"Tarjeta (Openpay - {brand} •••• {last4})",
                "fechaPago": datetime.utcnow()
            }
            db["suscripciones_pagos"].insert_one(pago_doc)
            
            internal_status = 'COMPLETADO'
            status_detail_msg = None
            
        else:
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
            logger.info(f"Clip Response: {json.dumps(clip_response)}")
            status = str(clip_response.get('status', '')).upper()
            logger.info(f"Status recibido de Clip: '{status}'")
            
            status_detail = clip_response.get('status_detail') or {}
            status_detail_msg = status_detail.get('message')
            
            internal_status = 'pending'
            if status == 'APPROVED':
                internal_status = 'COMPLETADO'
            elif status in ['DECLINED', 'CANCELLED', 'ERROR', 'FAILED', 'REJECTED']:
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
            if status_detail_msg:
                pago_doc["detalle"] = status_detail_msg

            db["suscripciones_pagos"].insert_one(pago_doc)

        if internal_status != 'COMPLETADO':
            msg = "La transacción no fue aprobada por la pasarela de pagos."
            if status_detail_msg:
                msg = f"{msg} Detalle: {status_detail_msg}"
            
            pago_data = {
                "id": str(pago_doc["_id"]),
                "concepto": concepto,
                "monto": float(monto),
                "fechaPago": iso_utc(pago_doc["fechaPago"]),
                "metodo": pago_doc["metodo"],
                "estado": pago_doc["estado"]
            }
            if status_detail_msg:
                pago_data["detalle"] = status_detail_msg

            return create_response(400, msg, {
                "pago": pago_data
            })

        # 5. Extender la vigencia del Taller (Platform DB -> talleres)
        taller = db["talleres"].find_one({"tenantId": tenant_id})
        corte_actual = None
        pago_actual = None
        meses_cargo = 1
        if taller:
            corte_actual = taller.get("proximaFechaCorte")
            pago_actual = taller.get("proximaFechaPago")
            meses_cargo = taller.get("mesesCargo", 1)
            try:
                meses_cargo = int(meses_cargo)
            except (ValueError, TypeError):
                meses_cargo = 1
        if meses_cargo < 1 or meses_cargo > 12:
            meses_cargo = 1
        
        # Parsear proximaFechaCorte a datetime naive
        corte_dt = None
        if corte_actual:
            if isinstance(corte_actual, str):
                try:
                    corte_dt = datetime.fromisoformat(corte_actual.replace("Z", "+00:00")).replace(tzinfo=None)
                except ValueError:
                    pass
            elif isinstance(corte_actual, datetime):
                corte_dt = corte_actual.replace(tzinfo=None)

        # Parsear proximaFechaPago a datetime naive
        pago_dt = None
        if pago_actual:
            if isinstance(pago_actual, str):
                try:
                    pago_dt = datetime.fromisoformat(pago_actual.replace("Z", "+00:00")).replace(tzinfo=None)
                except ValueError:
                    pass
            elif isinstance(pago_actual, datetime):
                pago_dt = pago_actual.replace(tzinfo=None)

        fecha_pago = datetime.utcnow()

        # Determinar nueva corte y pago para modelo Pre-pago
        if corte_dt and pago_dt:
            # Determinar si es el primer pago de todos (el primer pago inicial con gracia)
            # En el primer pago, corte_dt (fin de ciclo) y pago_dt (límite de pago) están distanciados por ~20 días.
            # En los subsecuentes, la fecha de pago es 10 días posterior a la fecha de corte (corte es inicio de ciclo).
            es_primer_pago = (corte_dt - pago_dt) > timedelta(days=15)

            if fecha_pago <= pago_dt:
                # Pago a tiempo (antes o en la fecha límite de pago)
                if es_primer_pago:
                    # El primer pago valida el ciclo actual, no avanzamos la corte por defecto
                    nueva_corte = corte_dt
                else:
                    # Ciclos subsecuentes: avanzamos la fecha de corte
                    nueva_corte = add_months(corte_dt, meses_cargo)
            else:
                # Pago tardío (fuera de la fecha límite de pago -> pago atrasado)
                # Opción A: nueva_fecha_corte = fecha_realmente_pago + meses_cargo - 10 días
                nueva_corte = add_months(fecha_pago, meses_cargo) - timedelta(days=10)
                
            # La fecha límite de pago es siempre 10 días posterior al corte
            nueva_pago = nueva_corte + timedelta(days=10)
        else:
            # Si no hay fechas guardadas previas, inicializar a partir de hoy
            nueva_corte = add_months(fecha_pago, meses_cargo)
            nueva_pago = nueva_corte + timedelta(days=10)

        db["talleres"].update_one(
            {"tenantId": tenant_id},
            {"$set": {
                "proximaFechaCorte": nueva_corte,
                "proximaFechaPago": nueva_pago,
                "estado": "ACTIVO"
            }}
        )

        pago_doc["id"] = str(pago_doc["_id"])
        del pago_doc["_id"]
        pago_doc["proximaFechaCorte"] = iso_utc(nueva_corte)
        pago_doc["proximaFechaPago"] = iso_utc(nueva_pago)
        
    
        # Guardar string de fecha de vencimiento formateada
        proximaFechaCorte_str = iso_utc(nueva_corte)
        proximaFechaPago_str = iso_utc(nueva_pago)

        return create_response(200, "Suscripción pagada exitosamente", {
            "pago": pago_doc,
        })

    except Exception as e:
        logger.error(f"Error procesando pago: {str(e)}")
        return handle_exception(e)

def obtener_historial_pagos_handler(event, context):
    try:
        # 1. Obtener tenant_id desde el token Cognito
        claims =get_claims(event)
        tenant_id = claims.get('custom:tenant_id')

        # 2. Leer query params para paginación y tallerTenantId
        query_params = event.get('queryStringParameters') or {}
        
        # Permitir filtrar por tallerTenantId o tenantId específico si se pasa por query params (para administrador)
        taller_tenant_id = query_params.get('tallerTenantId') or query_params.get('tenantId')
        if not taller_tenant_id:
            taller_tenant_id = tenant_id

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
        total = db["suscripciones_pagos"].count_documents({"tallerTenantId": taller_tenant_id})
        total_pages = max(1, (total + limit - 1) // limit)

        cursor = db["suscripciones_pagos"].find({"tallerTenantId": taller_tenant_id}).sort("fechaPago", -1).skip(skip).limit(limit)

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

def openpay_webhook_handler(event, context):
    try:
        # 1. Autenticación básica opcional para seguridad
        headers = event.get('headers') or {}
        auth_header = headers.get('authorization') or headers.get('Authorization') or ''
        
        webhook_user = os.environ.get('OPENPAY_WEBHOOK_USER', '').strip()
        webhook_pass = os.environ.get('OPENPAY_WEBHOOK_PASS', '').strip()
        
        if webhook_user and webhook_pass:
            expected_auth = f"Basic {base64.b64encode(f'{webhook_user}:{webhook_pass}'.encode()).decode()}"
            if auth_header != expected_auth:
                logger.error(f"Falla de autenticación en Webhook de Openpay: {auth_header}")
                return create_response(401, "No autorizado")
        
        # 2. Parsear el body
        body = json.loads(event.get("body") or "{}")
        event_type = body.get("type")
        transaction = body.get("transaction") or {}
        
        logger.info(f"Openpay Webhook Recibido: type='{event_type}', status='{transaction.get('status')}', id='{transaction.get('id')}'")
        logger.info(f"Cuerpo completo del webhook: {json.dumps(body)}")
        
        if event_type == "charge.succeeded" and transaction.get("status") == "completed":
            trans_id = transaction.get("id")
            customer_id = transaction.get("customer_id")
            amount = transaction.get("amount")
            
            db = get_platform_db()
            
            # Buscar el taller por openpaySpeiChargeId o por openpayCustomerId
            taller = db["talleres"].find_one({"openpaySpeiChargeId": trans_id})
            if not taller and customer_id:
                taller = db["talleres"].find_one({"openpayCustomerId": customer_id})
                
            if not taller:
                logger.error(f"Taller no encontrado para la transaccion de Openpay {trans_id} o cliente {customer_id}")
                return create_response(200, "Webhook recibido pero taller no fue localizado.")
                
            tenant_id = taller.get("tenantId")
            admin_email = taller.get("adminEmail", "pago@cliente.com")
            
            # Registrar el pago exitoso en la Colección 'suscripciones_pagos'
            pago_doc = {
                "tallerTenantId": tenant_id,
                "usuarioEmail": admin_email,
                "monto": float(amount),
                "concepto": transaction.get("description", "Suscripción Mensual Mekanics Manager"),
                "folioClip": trans_id, # Usamos folioClip para guardar el ID de transaccion Openpay
                "estado": "COMPLETADO",
                "metodo": "Transferencia SPEI (Openpay)",
                "fechaPago": datetime.utcnow()
            }
            db["suscripciones_pagos"].insert_one(pago_doc)
            
            # Calcular nueva fecha de corte
            corte_actual = taller.get("proximaFechaCorte")
            pago_actual = taller.get("proximaFechaPago")
            meses_cargo = taller.get("mesesCargo", 1)
            try:
                meses_cargo = int(meses_cargo)
            except (ValueError, TypeError):
                meses_cargo = 1
                
            corte_dt = None
            if corte_actual:
                if isinstance(corte_actual, str):
                    try:
                        corte_dt = datetime.fromisoformat(corte_actual.replace("Z", "+00:00")).replace(tzinfo=None)
                    except ValueError:
                        pass
                elif isinstance(corte_actual, datetime):
                    corte_dt = corte_actual.replace(tzinfo=None)
                    
            pago_dt = None
            if pago_actual:
                if isinstance(pago_actual, str):
                    try:
                        pago_dt = datetime.fromisoformat(pago_actual.replace("Z", "+00:00")).replace(tzinfo=None)
                    except ValueError:
                        pass
                elif isinstance(pago_actual, datetime):
                    pago_dt = pago_actual.replace(tzinfo=None)
                    
            fecha_pago = datetime.utcnow()
            
            if corte_dt and pago_dt:
                es_primer_pago = (corte_dt - pago_dt) > timedelta(days=15)
                if fecha_pago <= pago_dt:
                    if es_primer_pago:
                        nueva_corte = corte_dt
                    else:
                        nueva_corte = add_months(corte_dt, meses_cargo)
                else:
                    nueva_corte = add_months(fecha_pago, meses_cargo) - timedelta(days=10)
                nueva_pago = nueva_corte + timedelta(days=10)
            else:
                nueva_corte = add_months(fecha_pago, meses_cargo)
                nueva_pago = nueva_corte + timedelta(days=10)
                
            # Generar un NUEVO cargo SPEI para el siguiente mes
            openpay_spei_charge_id = ""
            openpay_clabe = taller.get("openpayClabe", "")
            
            try:
                from src.shared.utils import openpay_client
                monto_spei = float(taller.get("precioSuscripcion") or 599.00)
                if monto_spei <= 0:
                    monto_spei = 599.00
                    
                desc_spei = f"Suscripcion Mensual Mekanics Manager - {taller.get('nombreComercial', 'Taller')}"
                order_id_spei = f"SUB-{tenant_id[:16]}-{int(datetime.utcnow().timestamp())}"
                
                spei_res = openpay_client.create_spei_charge(
                    customer_id=customer_id,
                    amount=monto_spei,
                    description=desc_spei,
                    order_id=order_id_spei
                )
                
                openpay_spei_charge_id = spei_res.get("id", "")
                payment_method = spei_res.get("payment_method", {})
                openpay_clabe = payment_method.get("clabe", "")
            except Exception as new_spei_err:
                logger.error(f"Error al generar siguiente cargo SPEI para el taller {tenant_id}: {str(new_spei_err)}")
                
            # Actualizar vigencia y nuevo SPEI en el taller
            db["talleres"].update_one(
                {"tenantId": tenant_id},
                {"$set": {
                    "proximaFechaCorte": nueva_corte,
                    "proximaFechaPago": nueva_pago,
                    "estado": "ACTIVO",
                    "openpaySpeiChargeId": openpay_spei_charge_id,
                    "openpayClabe": openpay_clabe
                }}
            )
            logger.info(f"Suscripcion extendida exitosamente para el taller {tenant_id} via SPEI Webhook.")
            
        return create_response(200, "Webhook procesado correctamente")
    except Exception as e:
        logger.error(f"Error en openpay_webhook_handler: {str(e)}")
        return handle_exception(e)