import os
import json
import uuid
import boto3
from datetime import datetime
from aws_lambda_powertools import Logger
from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.infrastructure.database import get_platform_db, get_tenant_db

logger = Logger()
client = boto3.client('cognito-idp')
USER_POOL_ID = os.environ.get('COGNITO_USER_POOL_ID')

@logger.inject_lambda_context
def list_talleres_handler(event, context):
    try:
        db = get_platform_db()
        talleres_collection = db["talleres"]
        
        talleres_cursor = talleres_collection.find()
        
        talleres_list = []
        for taller in talleres_cursor:
            taller["id"] = str(taller["_id"])
            del taller["_id"]
            talleres_list.append(taller)
            
        return create_response(200, "Talleres obtenidos exitosamente", talleres_list)
    except Exception as e:
        return handle_exception(e)

@logger.inject_lambda_context
def create_taller_handler(event, context):
    try:
        body = json.loads(event.get("body") or "{}")
        required_fields = ["nombreComercial", "modeloLicencia", "adminEmail", "adminNombre", "adminApellido"]
        
        for field in required_fields:
            if field not in body:
                return create_response(400, f"Campo requerido faltante: {field}")
                
        tenant_id = uuid.uuid4().hex
        fecha_alta = body.get("fechaAlta", datetime.utcnow().isoformat())
        
        # 1. Create Cognito Admin User
        admin_email = body["adminEmail"]
        
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
            "modeloLicencia": body["modeloLicencia"],
            "estado": "ACTIVO",
            "fechaSuscripcion": fecha_alta,
            "adminEmail": admin_email,
            "adminNombre": body["adminNombre"],
            "adminApellido": body["adminApellido"]
        }
        
        platform_db = get_platform_db()
        result = platform_db["talleres"].insert_one(taller_doc)
        
        # 3. Initialize Tenant DB explicitly (MongoDB creates it upon first insertion)
        tenant_db = get_tenant_db(tenant_id)
        tenant_db["configuracion"].insert_one({
            "tenantId": tenant_id,
            "createdAt": datetime.utcnow().isoformat(),
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
            "createdAt": datetime.utcnow().isoformat()
        })
        
        taller_doc["id"] = str(result.inserted_id)
        del taller_doc["_id"]
        
        return create_response(201, "Taller creado exitosamente", taller_doc)
        
    except json.JSONDecodeError:
        return create_response(400, "Cuerpo de solicitud JSON inválido")
    except Exception as e:
        return handle_exception(e)
