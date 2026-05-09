import os
import json
import boto3
from typing import Dict, Any, List
from aws_lambda_powertools import Logger
from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.utils.auth_utils import parse_object_id
from src.shared.infrastructure.database import get_tenant_db

logger = Logger()
client = boto3.client('cognito-idp')

USER_POOL_ID = os.environ.get('COGNITO_USER_POOL_ID')
GROUPS = ['SUPER_ADMIN', 'ADMIN', 'ASESOR', 'MECANICO', 'CAJERO']

def parse_user_attributes(attributes: List[Dict[str, str]]) -> Dict[str, str]:
    return {attr['Name']: attr['Value'] for attr in attributes}

def get_icon_for_group(grupo: str) -> str:
    mapping = {
        'SUPER_ADMIN': 'ri-shield-user-line',
        'ADMIN': 'ri-user-settings-line',
        'ASESOR': 'ri-customer-service-2-line',
        'MECANICO': 'ri-tools-line',
        'CAJERO': 'ri-money-dollar-box-line'
    }
    return mapping.get(grupo, 'ri-user-3-line')

def format_user(cognito_user: Dict[str, Any], grupo: str = 'ASESOR') -> Dict[str, Any]:
    attrs = parse_user_attributes(cognito_user.get('Attributes', []))
    
    # Manejo robusto de la fecha
    raw_date = cognito_user.get('UserCreateDate', '')
    if hasattr(raw_date, 'isoformat'):
        created_at = raw_date.isoformat()
    else:
        created_at = str(raw_date)

    return {
        "email": attrs.get('email', cognito_user.get('Username', '')),
        "nombre": attrs.get('given_name', ''),
        "apellido": attrs.get('family_name', ''),
        "grupo": grupo,
        "icon": get_icon_for_group(grupo),
        "activo": cognito_user.get('Enabled', True),
        "telefono": attrs.get('phone_number', ''),
        "tenantId": attrs.get('custom:tenant_id', ''),
        "createdAt": created_at
    }

@logger.inject_lambda_context
def list_users_handler(event, context):
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        
        query_params = event.get('queryStringParameters') or {}
        grupo_filtro = query_params.get('grupo')
        
        tenant_db = get_tenant_db(tenant_id)
        
        query = {}
        if grupo_filtro:
            query['grupo'] = grupo_filtro
            
        logger.info(f"Buscando usuarios para tenant {tenant_id} con filtro: {query}")
        users_cursor = tenant_db["usuarios"].find(query)
        
        users = []
        for u in users_cursor:
            u["id"] = str(u["_id"])
            del u["_id"]
            users.append(u)
            
        return create_response(200, "Usuarios obtenidos", users)
    except Exception as e:
        return handle_exception(e)

@logger.inject_lambda_context
def create_user_handler(event, context):
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId válido para realizar esta acción.")
            
        body = json.loads(event.get('body', '{}'))
        email = body.get('email')
        nombre = body.get('nombre', '')
        apellido = body.get('apellido', '')
        grupo = body.get('grupo', 'ASESOR')
        telefono = body.get('telefono', '')

        # Valores por defecto para evitar fallos en Cognito attributes
        nombre = nombre if nombre else "Usuario"
        apellido = apellido if apellido else "SAE"

        user_attributes = [
            {'Name': 'email', 'Value': email},
            {'Name': 'email_verified', 'Value': 'true'},
            {'Name': 'given_name', 'Value': nombre},
            {'Name': 'family_name', 'Value': apellido},
            {'Name': 'custom:tenant_id', 'Value': str(tenant_id)}
        ]
        
        if telefono:
            if not telefono.startswith('+'):
                telefono = '+52' + telefono
            user_attributes.append({'Name': 'phone_number', 'Value': telefono})

        logger.info(f"DEBUG: Intentando crear en Cognito: {email} en Pool {USER_POOL_ID}")
        
        try:
            # PASO 1: CREACIÓN EN COGNITO
            response = client.admin_create_user(
                UserPoolId=USER_POOL_ID,
                Username=email,
                UserAttributes=user_attributes,
                DesiredDeliveryMediums=['EMAIL']
            )
            logger.info(f"✅ PASO 1: Usuario {email} creado en Cognito")
            
            # PASO 2: ASIGNACIÓN DE GRUPO (Opcional, no debe romper todo)
            try:
                client.admin_add_user_to_group(
                    UserPoolId=USER_POOL_ID,
                    Username=email,
                    GroupName=grupo
                )
                logger.info(f"✅ PASO 2: Grupo {grupo} asignado")
            except Exception as grp_err:
                logger.warning(f"⚠️ No se pudo asignar grupo {grupo} en Cognito: {str(grp_err)}")
            
        except Exception as cognito_err:
            logger.error(f"❌ FALLO CRITICO EN COGNITO: {str(cognito_err)}")
            # Error 400 si es un tema de parámetros, 500 si es otra cosa
            status = 400 if "InvalidParameter" in str(cognito_err) else 500
            return create_response(status, f"Error en Cognito: {str(cognito_err)}")

        user_data = format_user(response['User'], grupo)
        
        # PASO 3: PERSISTENCIA EN MONGO
        try:
            tenant_db = get_tenant_db(tenant_id)
            res_mongo = tenant_db["usuarios"].insert_one(user_data.copy())
            user_data["id"] = str(res_mongo.inserted_id)
            logger.info(f"✅ PASO 3: Usuario guardado en MongoDB")
        except Exception as mongo_err:
            logger.error(f"❌ FALLO EN MONGO: {str(mongo_err)}")
            # Si llegó aquí, el usuario ya existe en Cognito. Retornamos éxito pero avisamos del ID.
            user_data["id"] = "mongo_sync_error"
            
        return create_response(201, "Usuario creado exitosamente", user_data)
    except client.exceptions.UsernameExistsException:
        return create_response(400, "El correo electrónico ya está registrado en el sistema.")
    except Exception as e:
        logger.exception("FATAL: Error no controlado")
        return create_response(500, f"Error inesperado: {str(e)}")

@logger.inject_lambda_context
def update_user_handler(event, context):
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')

        if not tenant_id:
            return create_response(403, "No se encontró un tenantId válido para realizar esta acción.")

        user_id = event['pathParameters']['id']
        body = json.loads(event.get('body', '{}'))

        # El path id es el _id de Mongo; Cognito usa email como Username.
        object_id, err = parse_object_id(user_id)
        if err:
            return create_response(400, err)

        tenant_db = get_tenant_db(tenant_id)
        mongo_user = tenant_db["usuarios"].find_one({"_id": object_id})
        if not mongo_user:
            return create_response(404, "Usuario no encontrado.")

        cognito_username = mongo_user.get('email')
        if not cognito_username:
            return create_response(409, "El usuario no tiene email registrado, no se puede sincronizar con Cognito.")

        attributes = []
        if 'nombre' in body: attributes.append({'Name': 'given_name', 'Value': body['nombre']})
        if 'apellido' in body: attributes.append({'Name': 'family_name', 'Value': body['apellido']})

        if 'telefono' in body:
            telefono = body['telefono']
            if telefono and not telefono.startswith('+'):
                telefono = '+52' + telefono
            attributes.append({'Name': 'phone_number', 'Value': telefono})

        if attributes:
            client.admin_update_user_attributes(
                UserPoolId=USER_POOL_ID,
                Username=cognito_username,
                UserAttributes=attributes
            )

        if 'grupo' in body:
            new_group = body['grupo']
            try:
                groups_res = client.admin_list_groups_for_user(UserPoolId=USER_POOL_ID, Username=cognito_username)
                for g in groups_res.get('Groups', []):
                    client.admin_remove_user_from_group(
                        UserPoolId=USER_POOL_ID,
                        Username=cognito_username,
                        GroupName=g['GroupName']
                    )
                client.admin_add_user_to_group(
                    UserPoolId=USER_POOL_ID,
                    Username=cognito_username,
                    GroupName=new_group
                )
            except Exception as e:
                logger.warning(f"Error al cambiar el grupo del usuario {cognito_username}: {e}")

        if 'activo' in body:
            if body['activo']:
                client.admin_enable_user(UserPoolId=USER_POOL_ID, Username=cognito_username)
            else:
                client.admin_disable_user(UserPoolId=USER_POOL_ID, Username=cognito_username)

        update_data = {}
        if 'nombre' in body: update_data['nombre'] = body['nombre']
        if 'apellido' in body: update_data['apellido'] = body['apellido']
        if 'telefono' in body: update_data['telefono'] = body['telefono']
        if 'grupo' in body: 
            update_data['grupo'] = body['grupo']
            update_data['icon'] = get_icon_for_group(body['grupo'])
        if 'activo' in body: update_data['activo'] = body['activo']

        if update_data:
            tenant_db["usuarios"].update_one({"_id": object_id}, {"$set": update_data})

        body['id'] = user_id
        body['email'] = cognito_username
        return create_response(200, "Usuario actualizado", body)
    except Exception as e:
        return handle_exception(e)
