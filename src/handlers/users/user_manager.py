import os
import json
import boto3
from typing import Dict, Any, List
from aws_lambda_powertools import Logger
from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.infrastructure.database import get_tenant_db

logger = Logger()
client = boto3.client('cognito-idp')

USER_POOL_ID = os.environ.get('COGNITO_USER_POOL_ID')
GROUPS = ['SUPER_ADMIN', 'ADMIN', 'VENDEDOR', 'MECANICO']

def parse_user_attributes(attributes: List[Dict[str, str]]) -> Dict[str, str]:
    return {attr['Name']: attr['Value'] for attr in attributes}

def format_user(cognito_user: Dict[str, Any], grupo: str = 'VENDEDOR') -> Dict[str, Any]:
    attrs = parse_user_attributes(cognito_user.get('Attributes', []))
    
    return {
        "id": cognito_user.get('Username'),
        "email": attrs.get('email', ''),
        "nombre": attrs.get('given_name', ''),
        "apellido": attrs.get('family_name', ''),
        "grupo": grupo,
        "activo": cognito_user.get('Enabled', False),
        "telefono": attrs.get('phone_number', ''),
        "tenantId": attrs.get('custom:tenant_id', ''),
        "createdAt": str(cognito_user.get('UserCreateDate', ''))
    }

@logger.inject_lambda_context
def list_users_handler(event, context):
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado a este usuario.")
            
        tenant_db = get_tenant_db(tenant_id)
        users_cursor = tenant_db["usuarios"].find()
        
        users = []
        for u in users_cursor:
            if "_id" in u:
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
        grupo = body.get('grupo', 'VENDEDOR')
        telefono = body.get('telefono', '')

        if not email:
            return create_response(400, "El email es requerido")

        user_attributes = [
            {'Name': 'email', 'Value': email},
            {'Name': 'email_verified', 'Value': 'true'},
            {'Name': 'given_name', 'Value': nombre},
            {'Name': 'family_name', 'Value': apellido},
            {'Name': 'custom:tenant_id', 'Value': tenant_id}
        ]
        
        if telefono:
            if not telefono.startswith('+'):
                telefono = '+52' + telefono
            user_attributes.append({'Name': 'phone_number', 'Value': telefono})

        response = client.admin_create_user(
            UserPoolId=USER_POOL_ID,
            Username=email,
            UserAttributes=user_attributes,
            DesiredDeliveryMediums=['EMAIL']
        )
        
        client.admin_add_user_to_group(
            UserPoolId=USER_POOL_ID,
            Username=email,
            GroupName=grupo
        )

        user_data = format_user(response['User'], grupo)
        
        # Guardar copia en MongoDB del tenant
        tenant_db = get_tenant_db(tenant_id)
        tenant_db["usuarios"].insert_one(user_data.copy())

        if "_id" in user_data:
            del user_data["_id"]
            
        return create_response(201, "Usuario creado exitosamente", user_data)
    except client.exceptions.UsernameExistsException:
        return create_response(400, "El correo electrónico ingresado ya se encuentra registrado.")
    except Exception as e:
        return handle_exception(e)

@logger.inject_lambda_context
def update_user_handler(event, context):
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId válido para realizar esta acción.")

        user_id = event['pathParameters']['id']
        body = json.loads(event.get('body', '{}'))
        
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
                Username=user_id,
                UserAttributes=attributes
            )
            
        if 'grupo' in body:
            new_group = body['grupo']
            try:
                # Quitar grupos anteriores
                groups_res = client.admin_list_groups_for_user(UserPoolId=USER_POOL_ID, Username=user_id)
                for g in groups_res.get('Groups', []):
                    client.admin_remove_user_from_group(
                        UserPoolId=USER_POOL_ID,
                        Username=user_id,
                        GroupName=g['GroupName']
                    )
                # Asignar nuevo grupo
                client.admin_add_user_to_group(
                    UserPoolId=USER_POOL_ID,
                    Username=user_id,
                    GroupName=new_group
                )
            except Exception as e:
                logger.warning(f"Error al cambiar el grupo del usuario {user_id}: {e}")

        if 'activo' in body:
            if body['activo']:
                client.admin_enable_user(UserPoolId=USER_POOL_ID, Username=user_id)
            else:
                client.admin_disable_user(UserPoolId=USER_POOL_ID, Username=user_id)
        
        # Sincronizar actualización en MongoDB
        tenant_db = get_tenant_db(tenant_id)
        update_data = {}
        if 'nombre' in body: update_data['nombre'] = body['nombre']
        if 'apellido' in body: update_data['apellido'] = body['apellido']
        if 'telefono' in body: update_data['telefono'] = body['telefono']
        if 'grupo' in body: update_data['grupo'] = body['grupo']
        if 'activo' in body: update_data['activo'] = body['activo']
        
        if update_data:
            tenant_db["usuarios"].update_one({"id": user_id}, {"$set": update_data})
            
        body['id'] = user_id
        return create_response(200, "Usuario actualizado", body)
    except Exception as e:
        return handle_exception(e)
