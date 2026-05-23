import os
import json
import uuid
import boto3
from datetime import datetime
from aws_lambda_powertools import Logger
from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.utils.date_utils import iso_utc
from src.shared.infrastructure.database import get_platform_db, get_tenant_db

logger = Logger()
from botocore.config import Config
client = boto3.client('cognito-idp', config=Config(connect_timeout=5, read_timeout=15))
s3_client = boto3.client('s3', config=Config(connect_timeout=5, read_timeout=15))
USER_POOL_ID = os.environ.get('COGNITO_USER_POOL_ID')

# @logger.inject_lambda_context
def list_talleres_handler(event, context):
    try:
        db = get_platform_db()
        talleres_collection = db["talleres"]
        
        talleres_cursor = talleres_collection.find()
        
        talleres_list = []
        for taller in talleres_cursor:
            taller["id"] = str(taller["_id"])
            
            # Convertir todos los objetos datetime a ISO string de forma genérica
            for key, value in taller.items():
                if isinstance(value, datetime):
                    taller[key] = iso_utc(value)

            # Normalización para el frontend
            taller["modulos"] = taller.get("modulos", [])

            del taller["_id"]
            talleres_list.append(taller)
            
        return create_response(200, "Talleres recuperados", talleres_list)
    except Exception as e:
        return handle_exception(e)

# @logger.inject_lambda_context
def create_taller_handler(event, context):
    try:
        body = json.loads(event.get("body") or "{}")
        required_fields = ["nombreComercial", "adminEmail", "adminNombre", "adminApellido"]

        for field in required_fields:
            if field not in body:
                return create_response(400, f"Campo requerido faltante: {field}")

        admin_email = body["adminEmail"]
        # Validación mínima: presencia de "@" + dominio con punto.
        if "@" not in admin_email or "." not in admin_email.split("@")[-1]:
            return create_response(400, "El correo electrónico del administrador no es válido.")

        tenant_id = uuid.uuid4().hex
        fecha_alta = body.get("fechaAlta", iso_utc())

        # 1. Create Cognito Admin User
        try:
            client.admin_create_user(
                UserPoolId=USER_POOL_ID,
                Username=admin_email,
                UserAttributes=[
                    {'Name': 'email', 'Value': admin_email},
                    {'Name': 'email_verified', 'Value': 'true'},
                    {'Name': 'given_name', 'Value': body["adminNombre"]},
                    {'Name': 'family_name', 'Value': body["adminApellido"]},
                    {'Name': 'custom:tenant_id', 'Value': tenant_id}
                ],
                ForceAliasCreation=False,
                DesiredDeliveryMediums=['EMAIL']
            )
            
            client.admin_add_user_to_group(
                UserPoolId=USER_POOL_ID,
                Username=admin_email,
                GroupName='ADMIN'
            )
        except client.exceptions.UsernameExistsException:
            return create_response(400, "El correo electrónico del administrador ya está registrado.")
        except Exception as e:
            logger.error(f"Error creating Cognito user: {str(e)}")
            return create_response(500, "Error al crear el administrador del taller.")

        # 2. Insert into Platform DB
        taller_doc = {
            "tenantId": tenant_id,
            "nombreComercial": body["nombreComercial"],
            "direccion": body.get("direccion"),
            "modulos": body.get("modulos", []),
            "estado": body.get("estado", "ACTIVO"),
            "fechaSuscripcion": fecha_alta,
            "adminEmail": admin_email,
            "adminNombre": body["adminNombre"],
            "adminApellido": body["adminApellido"],
            "adminTelefono": body.get("adminTelefono"),
            "createdAt": datetime.utcnow()
        }
        
        platform_db = get_platform_db()
        result = platform_db["talleres"].insert_one(taller_doc)
        
        # 3. Initialize Tenant DB explicitly (MongoDB creates it upon first insertion)
        tenant_db = get_tenant_db(tenant_id)
        tenant_db["configuracion"].insert_one({
            "tenantId": tenant_id,
            "createdAt": iso_utc(),
            "status": "INITIALIZED"
        })
        
        # 4. Insert initial Admin into Tenant DB
        tenant_db["usuarios"].insert_one({
            "id": admin_email, # Will be replaced by Cognito Username if needed, but email is fine for now as it's the username
            "email": admin_email,
            "nombre": body["adminNombre"],
            "apellido": body["adminApellido"],
            "grupo": "ADMIN",
            "activo": True,
            "telefono": body.get("adminTelefono", ""),
            "tenantId": tenant_id,
            "createdAt": iso_utc()
        })

        taller_doc["id"] = str(result.inserted_id)
        if "createdAt" in taller_doc:
            taller_doc["createdAt"] = iso_utc(taller_doc["createdAt"])
        del taller_doc["_id"]
        
        return create_response(201, "Taller creado exitosamente", taller_doc)
        
    except json.JSONDecodeError:
        return create_response(400, "Cuerpo de solicitud JSON inválido")
    except Exception as e:
        logger.error(f"Error in create_taller: {str(e)}")
        return handle_exception(e)

def get_my_modulos_handler(event, context):
    """
    Obtiene los módulos habilitados para el taller del usuario logueado.
    """
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')

        if not tenant_id:
            # Si no hay tenant_id, probablemente es un SUPER_ADMIN global
            # Podríamos retornar todos los módulos o una lista vacía
            return create_response(200, "Módulos para admin global", {"modulos": ["*"]})

        db = get_platform_db()
        taller = db["talleres"].find_one({"tenantId": tenant_id}, {"_id": 0, "modulos": 1, "estado": 1, "logoUrl": 1, "nombreComercial": 1, "direccion": 1, "adminTelefono": 1})

        if not taller:
            return create_response(404, "Taller no encontrado")

        return create_response(200, "Configuración recuperada", {
            "modulos": taller.get("modulos", []),
            "estado": taller.get("estado", "ACTIVO"),
            "logoUrl": taller.get("logoUrl"),
            "nombreTaller": taller.get("nombreComercial", "SIGA"),
            "direccion": taller.get("direccion", "Dirección no especificada"),
            "adminTelefono": taller.get("adminTelefono", "Teléfono no especificado")
        })

    except Exception as e:
        return handle_exception(e)

def update_taller_handler(event, context):
    """
    Actualiza la información de un taller existente.
    """
    try:
        taller_id = event.get('pathParameters', {}).get('id')
        if not taller_id:
            return create_response(400, "ID de taller no proporcionado")

        body = json.loads(event.get("body") or "{}")
        db = get_platform_db()
        
        from bson import ObjectId
        
        update_data = {
            "nombreComercial": body.get("nombreComercial"),
            "direccion": body.get("direccion"),
            "modulos": body.get("modulos", []),
            "adminNombre": body.get("adminNombre"),
            "adminApellido": body.get("adminApellido"),
            "adminTelefono": body.get("adminTelefono"),
            "estado": body.get("estado"),
            "updatedAt": datetime.utcnow()
        }

        # Eliminar Nones para no sobreescribir con vacío si no se enviaron
        update_data = {k: v for k, v in update_data.items() if v is not None}

        result = db["talleres"].update_one(
            {"_id": ObjectId(taller_id)},
            {"$set": update_data}
        )

        if result.matched_count == 0:
            return create_response(404, "Taller no encontrado")

        # Recuperar el taller actualizado para devolverlo
        updated_taller = db["talleres"].find_one({"_id": ObjectId(taller_id)})
        updated_taller["id"] = str(updated_taller["_id"])
        del updated_taller["_id"]
        
        # Normalizar fechas
        for key, value in updated_taller.items():
            if isinstance(value, datetime):
                updated_taller[key] = iso_utc(value)

        return create_response(200, "Taller actualizado exitosamente", updated_taller)

    except Exception as e:
        logger.error(f"Error in update_taller: {str(e)}")
        return handle_exception(e)

def upload_logo_handler(event, context):
    """
    Recibe una imagen en base64, la redimensiona y la guarda en S3.
    """
    try:
        from bson import ObjectId
        from PIL import Image
        import io
        import base64

        logger.info("Iniciando carga de logotipo")
        taller_id = event.get('pathParameters', {}).get('id')
        if not taller_id:
            return create_response(400, "ID de taller no proporcionado")

        # Intentar obtener el cuerpo de forma segura
        body_raw = event.get("body")
        if event.get("isBase64Encoded"):
            body_raw = base64.b64decode(body_raw).decode('utf-8')
        
        body = json.loads(body_raw or "{}")
        image_base64 = body.get("image")

        if not image_base64:
            logger.warning("No se proporcionó imagen en el cuerpo")
            return create_response(400, "Imagen no proporcionada")

        # Limpiar prefijo base64 si existe (data:image/png;base64,...)
        if "," in image_base64:
            image_base64 = image_base64.split(",")[1]

        # 1. Obtener información del taller
        db = get_platform_db()
        try:
            obj_id = ObjectId(taller_id)
        except Exception:
            logger.error(f"ID de taller inválido: {taller_id}")
            return create_response(400, "ID de taller inválido")

        taller = db["talleres"].find_one({"_id": obj_id})
        if not taller:
            return create_response(404, "Taller no encontrado")

        tenant_id = taller["tenantId"]
        logger.info(f"Procesando logo para tenant: {tenant_id}")

        # 2. Procesar imagen con Pillow
        try:
            image_data = base64.b64decode(image_base64)
            img = Image.open(io.BytesIO(image_data))
            
            # Convertir a RGB si es necesario
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB") # Cambiado a RGB para mayor compatibilidad
            
            # Redimensionar (máximo 400x200 manteniendo proporción)
            img.thumbnail((400, 200))
            
            # Guardar en buffer
            output = io.BytesIO()
            img.save(output, format='JPEG', quality=85) # Cambiado a JPEG para simplificar
            output.seek(0)
            content_type = 'image/jpeg'
            ext = 'jpg'
        except Exception as img_err:
            logger.error(f"Error procesando imagen con Pillow: {str(img_err)}")
            return create_response(400, f"Error al procesar la imagen: {str(img_err)}")

        # 3. Subir a S3
        s3 = s3_client
        bucket = os.environ.get('S3_MEDIA_BUCKET')
        key = f"logotipos/logo_{tenant_id}.{ext}"

        logger.info(f"Subiendo a S3: {bucket}/{key}")
        
        # Subir sin ACL público para evitar errores de Block Public Access
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=output,
            ContentType=content_type
        )

        logo_url = f"https://{bucket}.s3.amazonaws.com/{key}?t={int(datetime.utcnow().timestamp())}"

        # 4. Actualizar URL en BD
        db["talleres"].update_one(
            {"_id": obj_id},
            {"$set": {"logoUrl": logo_url}}
        )

        logger.info("Logotipo actualizado exitosamente")
        return create_response(200, "Logotipo actualizado", {"logoUrl": logo_url})

    except Exception as e:
        logger.error(f"Error in upload_logo: {str(e)}")
        return handle_exception(e)
