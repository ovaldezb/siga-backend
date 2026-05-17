import os
import json
import boto3
from typing import Dict, Any, List
from aws_lambda_powertools import Logger
from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.utils.auth_utils import parse_object_id
from src.shared.infrastructure.database import get_tenant_db
from src.shared.utils.date_utils import iso_utc

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
        created_at = iso_utc(raw_date)
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

def populate_user_sucursales(user, sucursales_map):
    raw = user.get("sucursales", [])
    populated = []
    valid_refs = []
    for item in raw:
        sid = item.get("sucursal")
        if sid and sid in sucursales_map:
            populated.append(sucursales_map[sid])
            valid_refs.append(item)
    
    # Limpiamos el objeto original para no arrastrar referencias muertas
    user["sucursales"] = populated
    return user

def get_sucursales_map(tenant_db):
    from datetime import datetime
    sucursales_cursor = list(tenant_db["sucursales"].find())
    sucursales_map = {}
    for s in sucursales_cursor:
        sid = str(s.pop('_id'))
        s['id'] = sid
        for k, v in s.items():
            if isinstance(v, datetime):
                s[k] = iso_utc(v)
        sucursales_map[sid] = s
    return sucursales_map

@logger.inject_lambda_context
def list_users_handler(event, context):
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        
        tenant_db = get_tenant_db(tenant_id)
        sucursales_map = get_sucursales_map(tenant_db)

        query_params = event.get('queryStringParameters') or {}
        grupo_filtro = query_params.get('grupo')
        query = {}
        if grupo_filtro:
            query['grupo'] = grupo_filtro
            
        users_cursor = tenant_db["usuarios"].find(query)
        users = []
        for u in users_cursor:
            u["id"] = str(u["_id"])
            del u["_id"]
            users.append(populate_user_sucursales(u, sucursales_map))
            
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
        sucursales = body.get('sucursales', [])

        user_attributes = [
            {'Name': 'email', 'Value': email},
            {'Name': 'email_verified', 'Value': 'true'},
            {'Name': 'given_name', 'Value': nombre if nombre else "Usuario"},
            {'Name': 'family_name', 'Value': apellido if apellido else "SAE"},
            {'Name': 'custom:tenant_id', 'Value': str(tenant_id)}
        ]
        
        if telefono:
            clean_tel = "".join(filter(str.isdigit, telefono))
            if len(clean_tel) >= 10:
                telefono_cognito = clean_tel if clean_tel.startswith('+') else '+52' + clean_tel
                user_attributes.append({'Name': 'phone_number', 'Value': telefono_cognito})

        # Validaciones previas
        if not email:
            return create_response(400, "El campo 'email' es obligatorio.")
        if grupo not in GROUPS:
            return create_response(400, f"Grupo inválido. Valores permitidos: {', '.join(GROUPS)}")

        response = client.admin_create_user(
            UserPoolId=USER_POOL_ID,
            Username=email,
            UserAttributes=user_attributes,
            DesiredDeliveryMediums=['EMAIL']
        )

        try:
            client.admin_add_user_to_group(UserPoolId=USER_POOL_ID, Username=email, GroupName=grupo)
        except Exception as grp_err:
            logger.warning(f"No se pudo agregar usuario {email} al grupo {grupo}: {grp_err}")
            
        user_data = format_user(response['User'], grupo)
        user_data['sucursales'] = sucursales
        
        tenant_db = get_tenant_db(tenant_id)
        res_mongo = tenant_db["usuarios"].insert_one(user_data.copy())
        user_data["id"] = str(res_mongo.inserted_id)
        
        # Devolver poblado
        sucursales_map = get_sucursales_map(tenant_db)
        return create_response(201, "Usuario creado", populate_user_sucursales(user_data, sucursales_map))
        
    except client.exceptions.UsernameExistsException:
        return create_response(400, "El correo electrónico ya está registrado.")
    except Exception as e:
        return handle_exception(e)

@logger.inject_lambda_context
def update_user_handler(event, context):
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        user_id = event['pathParameters']['id']
        body = json.loads(event.get('body', '{}'))

        object_id, err = parse_object_id(user_id)
        if err: return create_response(400, err)

        tenant_db = get_tenant_db(tenant_id)
        mongo_user = tenant_db["usuarios"].find_one({"_id": object_id})
        if not mongo_user: return create_response(404, "No encontrado.")

        email = mongo_user.get('email')
        attributes = []
        if 'nombre' in body: attributes.append({'Name': 'given_name', 'Value': body['nombre']})
        if 'apellido' in body: attributes.append({'Name': 'family_name', 'Value': body['apellido']})
        if 'telefono' in body:
            tel = body['telefono']
            attributes.append({'Name': 'phone_number', 'Value': tel if tel.startswith('+') else '+52' + tel})

        if attributes:
            client.admin_update_user_attributes(UserPoolId=USER_POOL_ID, Username=email, UserAttributes=attributes)

        if 'grupo' in body:
            if body['grupo'] not in GROUPS:
                return create_response(400, f"Grupo inválido. Valores permitidos: {', '.join(GROUPS)}")
            try:
                groups_res = client.admin_list_groups_for_user(UserPoolId=USER_POOL_ID, Username=email)
                for g in groups_res.get('Groups', []):
                    client.admin_remove_user_from_group(UserPoolId=USER_POOL_ID, Username=email, GroupName=g['GroupName'])
                client.admin_add_user_to_group(UserPoolId=USER_POOL_ID, Username=email, GroupName=body['grupo'])
            except Exception as grp_err:
                logger.warning(f"No se pudo cambiar grupo de {email} a {body['grupo']}: {grp_err}")

        if 'activo' in body:
            if body['activo']: client.admin_enable_user(UserPoolId=USER_POOL_ID, Username=email)
            else: client.admin_disable_user(UserPoolId=USER_POOL_ID, Username=email)

        update_data = {}
        for k in ['nombre', 'apellido', 'telefono', 'grupo', 'activo', 'sucursales']:
            if k in body: update_data[k] = body[k]
        if 'grupo' in body: update_data['icon'] = get_icon_for_group(body['grupo'])

        if update_data:
            tenant_db["usuarios"].update_one({"_id": object_id}, {"$set": update_data})

        # Recuperar usuario completo y devolverlo poblado
        updated_user = tenant_db["usuarios"].find_one({"_id": object_id})
        updated_user["id"] = str(updated_user.pop("_id"))
        sucursales_map = get_sucursales_map(tenant_db)
        
        return create_response(200, "Actualizado", populate_user_sucursales(updated_user, sucursales_map))
    except Exception as e:
        return handle_exception(e)

@logger.inject_lambda_context
def get_me_handler(event, context):
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        email = claims.get('email')
        
        tenant_db = get_tenant_db(tenant_id)
        user = tenant_db["usuarios"].find_one({"email": email})
        
        if not user:
            return create_response(404, "Usuario no encontrado")
            
        user["id"] = str(user.pop("_id"))
        sucursales_map = get_sucursales_map(tenant_db)
        
        return create_response(200, "Perfil obtenido", populate_user_sucursales(user, sucursales_map))
    except Exception as e:
        return handle_exception(e)
