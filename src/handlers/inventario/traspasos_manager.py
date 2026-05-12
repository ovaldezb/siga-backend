import json
from bson import ObjectId
from datetime import datetime
from aws_lambda_powertools import Logger
from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.infrastructure.database import get_tenant_db

logger = Logger()

@logger.inject_lambda_context
def create_traspaso_handler(event, context):
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        usuario_id = claims.get('sub')
        usuario_nombre = claims.get('name', 'Usuario Desconocido')

        body = json.loads(event.get('body', '{}'))
        origen_id = body.get('origen_id')
        destino_id = body.get('destino_id')
        items = body.get('items', []) # [{"item_id": "...", "cantidad": 5}]

        if not origen_id or not destino_id or not items:
            return create_response(400, "Origen, destino y items son requeridos")

        db = get_tenant_db(tenant_id)

        # 1. Descontar stock de origen
        for item in items:
            db["inventario"].update_one(
                {"_id": ObjectId(item['item_id']), "tenant_id": tenant_id, "sucursal_id": origen_id},
                {"$inc": {"stock": -item['cantidad']}}
            )

        # 2. Crear registro de traspaso en tránsito
        doc = {
            "tenant_id": tenant_id,
            "origen_id": origen_id,
            "destino_id": destino_id,
            "items": items,
            "estado": "EN_TRANSITO",
            "creado_por": {"id": usuario_id, "nombre": usuario_nombre},
            "createdAt": datetime.utcnow(),
            "updatedAt": datetime.utcnow()
        }

        result = db["traspasos"].insert_one(doc)
        doc["id"] = str(result.inserted_id)
        del doc["_id"]
        doc["createdAt"] = doc["createdAt"].isoformat()
        doc["updatedAt"] = doc["updatedAt"].isoformat()

        # Opcional: Crear notificación para la sucursal destino en la colección de autorizaciones/notificaciones

        return create_response(201, "Traspaso creado y en tránsito", doc)
    except Exception as e:
        return handle_exception(e)

@logger.inject_lambda_context
def list_traspasos_handler(event, context):
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')

        query_params = event.get('queryStringParameters') or {}
        sucursal_id = query_params.get('sucursal_id')
        tipo = query_params.get('tipo', 'entrantes') # entrantes, salientes, todos
        estado = query_params.get('estado')

        filter_query = {"tenant_id": tenant_id}
        
        if sucursal_id:
            if tipo == 'entrantes':
                filter_query["destino_id"] = sucursal_id
            elif tipo == 'salientes':
                filter_query["origen_id"] = sucursal_id
            else:
                filter_query["$or"] = [{"destino_id": sucursal_id}, {"origen_id": sucursal_id}]

        if estado:
            filter_query["estado"] = estado

        db = get_tenant_db(tenant_id)
        cursor = db["traspasos"].find(filter_query).sort("createdAt", -1).limit(50)
        
        traspasos = []
        for doc in cursor:
            doc['id'] = str(doc.pop('_id'))
            if 'createdAt' in doc and isinstance(doc['createdAt'], datetime):
                doc['createdAt'] = doc['createdAt'].isoformat()
            if 'updatedAt' in doc and isinstance(doc['updatedAt'], datetime):
                doc['updatedAt'] = doc['updatedAt'].isoformat()
            traspasos.append(doc)

        return create_response(200, "Traspasos", {"items": traspasos})
    except Exception as e:
        return handle_exception(e)

@logger.inject_lambda_context
def receive_traspaso_handler(event, context):
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        usuario_id = claims.get('sub')
        usuario_nombre = claims.get('name', 'Usuario Desconocido')

        traspaso_id = event['pathParameters']['id']
        body = json.loads(event.get('body', '{}'))
        estado = body.get('estado') # COMPLETADO, PARCIAL, RECHAZADO
        items_recibidos = body.get('items_recibidos', []) # [{item_id, cantidad_recibida, merma}]

        db = get_tenant_db(tenant_id)
        
        traspaso = db["traspasos"].find_one({"_id": ObjectId(traspaso_id), "tenant_id": tenant_id})
        if not traspaso:
            return create_response(404, "Traspaso no encontrado")

        if traspaso['estado'] != 'EN_TRANSITO':
            return create_response(400, "El traspaso no está en tránsito")

        destino_id = traspaso['destino_id']

        if estado in ['COMPLETADO', 'PARCIAL']:
            # Sumar stock al destino según lo recibido
            for rec in items_recibidos:
                item_id = rec.get('item_id')
                cant_recibida = rec.get('cantidad_recibida', 0)
                if cant_recibida > 0:
                    # Buscar si el item ya existe en la sucursal destino
                    # (Si no existe, habría que crearlo, pero simplificamos asumiendo que el item existe o se actualiza por no_parte)
                    db["inventario"].update_one(
                        {"_id": ObjectId(item_id), "tenant_id": tenant_id, "sucursal_id": destino_id},
                        {"$inc": {"stock": cant_recibida}}
                    )

        update_data = {
            "estado": estado,
            "recibido_por": {"id": usuario_id, "nombre": usuario_nombre},
            "items_recibidos": items_recibidos,
            "updatedAt": datetime.utcnow()
        }

        db["traspasos"].update_one(
            {"_id": ObjectId(traspaso_id)},
            {"$set": update_data}
        )

        return create_response(200, "Recepción registrada exitosamente")
    except Exception as e:
        return handle_exception(e)
