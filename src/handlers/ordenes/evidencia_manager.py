import os
import json
import boto3
from datetime import datetime
from aws_lambda_powertools import Logger
from src.shared.infrastructure.database import get_tenant_db
from src.shared.utils.response_handler import create_response, handle_exception
from bson import ObjectId

logger = Logger()

def get_upload_url_handler(event, context):
    """
    Genera una URL firmada para subir un archivo directamente a S3.
    """
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        orden_id = event.get('pathParameters', {}).get('id')
        
        body = json.loads(event.get('body') or '{}')
        file_name = body.get('fileName')
        file_type = body.get('fileType', 'video/mp4')
        
        if not tenant_id or not orden_id or not file_name:
            return create_response(400, "Parámetros insuficientes")

        logger.info(f"[EVIDENCE V3] Generando URL para orden {orden_id}")
        db = get_tenant_db(tenant_id)
        orden = db["ordenes"].find_one({"_id": ObjectId(orden_id)}, {"folio": 1})
        
        if not orden:
            logger.warning(f"Orden {orden_id} no encontrada para el tenant {tenant_id}")
            folio = orden_id
        else:
            folio = orden.get("folio", orden_id)
            logger.info(f"Generando URL para orden {orden_id} con folio {folio}")

        # Ajustar tenant_id (usar la mitad si es muy grande)
        short_tenant = tenant_id
        if len(tenant_id) > 16:
            short_tenant = tenant_id[:len(tenant_id)//2]
            logger.info(f"Tenant ID truncado de {len(tenant_id)} a {len(short_tenant)} caracteres")

        s3 = boto3.client('s3')
        bucket = os.environ.get('S3_EVIDENCIA_BUCKET')
        
        # Estructura REQUERIDA: {SHORT_TENANT}/{FOLIO}/{file_name}
        # Limpiar nombre de archivo para evitar problemas en S3
        safe_file_name = "".join([c if c.isalnum() or c in "._-" else "_" for c in file_name])
        
        # FORZAR ELIMINACION DE CUALQUIER PREFIJO ANTERIOR
        key = f"{short_tenant}/{folio}/{safe_file_name}"
        logger.info(f"[EVIDENCE V3] KEY FINAL: {key}")
        
        url = s3.generate_presigned_url(
            'put_object',
            Params={
                'Bucket': bucket,
                'Key': key,
                'ContentType': file_type
            },
            ExpiresIn=3600 # 1 hora
        )
        
        return create_response(200, "URL generada", {
            "uploadUrl": url,
            "key": key
        })
        
    except Exception as e:
        logger.error(f"Error generating upload URL: {str(e)}", exc_info=True)
        return handle_exception(e)

def add_evidencia_handler(event, context):
    """
    Registra una nueva evidencia en la base de datos después de haber sido subida a S3.
    """
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        orden_id = event.get('pathParameters', {}).get('id')
        
        body = json.loads(event.get('body') or '{}')
        s3_key = body.get('s3Key')
        nombre_original = body.get('nombreOriginal')
        
        if not tenant_id or not orden_id or not s3_key:
            return create_response(400, "Parámetros insuficientes")
            
        logger.info(f"Registrando evidencia para orden {orden_id} (Tenant: {tenant_id})")
        db = get_tenant_db(tenant_id)
        
        # Fecha formateada según solicitud del usuario
        fecha_str = datetime.now().strftime('%d-%m-%Y_%H:%M')
        
        nueva_evidencia = {
            "fecha": fecha_str,
            "s3Key": s3_key,
            "nombreOriginal": nombre_original,
            "createdAt": datetime.utcnow()
        }
        
        result = db["ordenes"].update_one(
            {"_id": ObjectId(orden_id)},
            {"$push": {"evidencia": nueva_evidencia}}
        )
        
        if result.matched_count == 0:
            logger.warning(f"No se encontró la orden {orden_id} para registrar evidencia")
            return create_response(404, "Orden no encontrada en la base de datos")

        logger.info(f"Evidencia registrada exitosamente en orden {orden_id}")
        return create_response(200, "Evidencia registrada", nueva_evidencia)
        
    except Exception as e:
        logger.error(f"Error adding evidencia: {str(e)}", exc_info=True)
        return handle_exception(e)

def list_evidencia_handler(event, context):
    """
    Lista las evidencias de una orden de servicio, generando URLs temporales de visualización.
    """
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        orden_id = event.get('pathParameters', {}).get('id')
        
        if not tenant_id or not orden_id:
            return create_response(400, "Parámetros insuficientes")
            
        db = get_tenant_db(tenant_id)
        orden = db["ordenes"].find_one({"_id": ObjectId(orden_id)}, {"evidencia": 1})
        
        if not orden:
            return create_response(404, "Orden no encontrada")
            
        evidencias = orden.get("evidencia", [])
        s3 = boto3.client('s3')
        bucket = os.environ.get('S3_EVIDENCIA_BUCKET')
        
        for ev in evidencias:
            # Generar URL de visualización temporal
            try:
                view_url = s3.generate_presigned_url(
                    'get_object',
                    Params={
                        'Bucket': bucket,
                        'Key': ev['s3Key']
                    },
                    ExpiresIn=3600
                )
                ev['url'] = view_url
            except Exception as s3_err:
                logger.warning(f"Could not generate view URL for key {ev.get('s3Key')}: {str(s3_err)}")
                ev['url'] = None
            
            # Formatear fechas para JSON
            if 'createdAt' in ev and isinstance(ev['createdAt'], datetime):
                ev['createdAt'] = ev['createdAt'].isoformat()
                
        return create_response(200, "Evidencias recuperadas", evidencias)
        
    except Exception as e:
        logger.error(f"Error listing evidencias: {str(e)}")
        return handle_exception(e)
