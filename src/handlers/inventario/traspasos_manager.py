from src.shared.utils.auth_utils import get_claims
import json
from bson import ObjectId
from datetime import datetime
from aws_lambda_powertools import Logger
from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.infrastructure.database import get_tenant_db
from src.shared.utils.date_utils import iso_utc

logger = Logger()

@logger.inject_lambda_context
def create_traspaso_handler(event, context):
    try:
        claims =get_claims(event)
        tenant_id = claims.get('custom:tenant_id')
        usuario_id = claims.get('sub')
        usuario_nombre = claims.get('name') or claims.get('email') or 'Usuario Desconocido'

        body = json.loads(event.get('body', '{}'))
        origen_id = body.get('origen_id')
        destino_id = body.get('destino_id')
        items = body.get('items', [])  # [{"item_id": "...", "cantidad": 5}]

        if not origen_id or not destino_id or not items:
            return create_response(400, "Origen, destino y items son requeridos")
        if origen_id == destino_id:
            return create_response(400, "El origen y el destino no pueden ser la misma sucursal.")

        db = get_tenant_db(tenant_id)

        # 1. Validar stock disponible en origen ANTES de descontar (evita traspasos negativos)
        for item in items:
            try:
                cantidad = int(item.get('cantidad', 0))
            except (TypeError, ValueError):
                return create_response(400, f"Cantidad inválida para item {item.get('item_id')}")
            if cantidad <= 0:
                return create_response(400, "Las cantidades de traspaso deben ser mayores a cero.")
            doc_item = db["items"].find_one({"_id": ObjectId(item['item_id']), "sucursal_id": origen_id})
            if not doc_item:
                return create_response(404, f"Item {item.get('item_id')} no existe en la sucursal origen.")
            if int(doc_item.get('stock', 0)) < cantidad:
                return create_response(400, f"Stock insuficiente en origen para {doc_item.get('nombre')}: {doc_item.get('stock', 0)} disponibles, solicitas {cantidad}.")

        # 2. Descontar stock de origen (atómico con guard $gte para evitar carrera)
        descontados = []
        try:
            for item in items:
                cantidad = int(item['cantidad'])
                res = db["items"].update_one(
                    {"_id": ObjectId(item['item_id']), "sucursal_id": origen_id, "stock": {"$gte": cantidad}},
                    {"$inc": {"stock": -cantidad}}
                )
                if res.modified_count != 1:
                    raise RuntimeError(f"Race condition: stock cambió mientras se procesaba el traspaso para {item['item_id']}")
                descontados.append(item)
        except Exception as dec_err:
            # Rollback de lo que sí descontamos
            for d in descontados:
                db["items"].update_one(
                    {"_id": ObjectId(d['item_id']), "sucursal_id": origen_id},
                    {"$inc": {"stock": int(d['cantidad'])}}
                )
            return create_response(409, f"No se pudo crear el traspaso: {dec_err}")

        # 3. Snapshot de no_parte/nombre para que el destino pueda recibir aunque no tenga el item.
        #    Aprovechamos para armar la bitácora de salida (el stock de origen_doc ya está descontado).
        items_enriched = []
        movimientos_salida = []
        for it in items:
            origen_doc = db["items"].find_one({"_id": ObjectId(it['item_id']), "sucursal_id": origen_id})
            cantidad = int(it['cantidad'])
            items_enriched.append({
                "item_id": str(it['item_id']),
                "cantidad": cantidad,
                "no_parte": (origen_doc or {}).get('no_parte'),
                "nombre": (origen_doc or {}).get('nombre'),
                "precio_compra": (origen_doc or {}).get('precio_compra', 0),
            })
            stock_resultante = int((origen_doc or {}).get('stock', 0))
            movimientos_salida.append({
                "tenant_id": tenant_id,
                "item_id": str(it['item_id']),
                "item_nombre": (origen_doc or {}).get('nombre'),
                "sucursal_id": origen_id,
                "cantidad": -cantidad,
                "stock_anterior": stock_resultante + cantidad,
                "stock_resultante": stock_resultante,
                "concepto": "TRASPASO_SALIDA",
                "usuario_id": usuario_id,
                "usuario_nombre": usuario_nombre,
                "createdAt": datetime.utcnow(),
            })

        # 4. Crear registro de traspaso en tránsito
        doc = {
            "tenant_id": tenant_id,
            "origen_id": origen_id,
            "destino_id": destino_id,
            "items": items_enriched,
            "estado": "EN_TRANSITO",
            "creado_por": {"id": usuario_id, "nombre": usuario_nombre},
            "createdAt": datetime.utcnow(),
            "updatedAt": datetime.utcnow()
        }

        result = db["traspasos"].insert_one(doc)
        doc["id"] = str(result.inserted_id)
        del doc["_id"]
        doc["createdAt"] = iso_utc(doc["createdAt"])
        doc["updatedAt"] = iso_utc(doc["updatedAt"])

        # 5. Bitácora de inventario (salida). No es fatal si falla la escritura.
        try:
            for m in movimientos_salida:
                m["referencia_id"] = doc["id"]
            if movimientos_salida:
                db["inventario_movimientos"].insert_many(movimientos_salida)
        except Exception as bit_err:
            logger.warning(f"No se pudo registrar bitácora de traspaso (salida): {bit_err}")

        return create_response(201, "Traspaso creado y en tránsito", doc)
    except Exception as e:
        return handle_exception(e)

@logger.inject_lambda_context
def list_traspasos_handler(event, context):
    try:
        claims =get_claims(event)
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
                doc['createdAt'] = iso_utc(doc['createdAt'])
            if 'updatedAt' in doc and isinstance(doc['updatedAt'], datetime):
                doc['updatedAt'] = iso_utc(doc['updatedAt'])
            traspasos.append(doc)

        return create_response(200, "Traspasos", {"items": traspasos})
    except Exception as e:
        return handle_exception(e)

@logger.inject_lambda_context
def receive_traspaso_handler(event, context):
    try:
        claims =get_claims(event)
        tenant_id = claims.get('custom:tenant_id')
        usuario_id = claims.get('sub')
        usuario_nombre = claims.get('name') or claims.get('email') or 'Usuario Desconocido'

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
            # Mapa de snapshot por item_id para poder clonar si el item no existe en destino
            snapshot_by_id = {str(s.get('item_id')): s for s in (traspaso.get('items') or [])}

            movimientos_entrada = []
            for rec in items_recibidos:
                item_id = rec.get('item_id')
                try:
                    cant_recibida = int(rec.get('cantidad_recibida', 0))
                except (TypeError, ValueError):
                    cant_recibida = 0
                if cant_recibida <= 0:
                    continue

                snap = snapshot_by_id.get(str(item_id), {})
                no_parte = snap.get('no_parte')

                # 1) Sumar stock por _id si el item ya existe en destino. find_one_and_update
                #    con return_document=True nos da el stock resultante para la bitácora.
                destino_doc = db["items"].find_one_and_update(
                    {"_id": ObjectId(item_id), "sucursal_id": destino_id},
                    {"$inc": {"stock": cant_recibida}},
                    return_document=True,
                )

                # 2) Reconciliar por no_parte: misma SKU pero ya existe en destino con otro _id
                if not destino_doc and no_parte:
                    destino_doc = db["items"].find_one_and_update(
                        {"no_parte": no_parte, "sucursal_id": destino_id},
                        {"$inc": {"stock": cant_recibida}},
                        return_document=True,
                    )

                # 3) Clonar el item del origen para crear stock inicial en destino
                if not destino_doc:
                    origen_doc = db["items"].find_one({"_id": ObjectId(item_id)})
                    if origen_doc:
                        clone = {k: v for k, v in origen_doc.items() if k != '_id'}
                        clone["sucursal_id"] = destino_id
                        clone["stock"] = cant_recibida
                        clone["createdAt"] = iso_utc()
                        clone["clonado_de"] = str(origen_doc['_id'])
                        try:
                            ins = db["items"].insert_one(clone)
                            clone["_id"] = ins.inserted_id
                            destino_doc = clone
                        except Exception as clone_err:
                            logger.warning(f"No se pudo clonar item {item_id} a sucursal {destino_id}: {clone_err}")
                    else:
                        logger.warning(f"Recepción de traspaso: item {item_id} no existe en origen ni destino; stock perdido")

                # Bitácora de entrada por este item recibido.
                if destino_doc:
                    stock_resultante = int(destino_doc.get('stock', 0))
                    movimientos_entrada.append({
                        "tenant_id": tenant_id,
                        "item_id": str(destino_doc.get('_id', item_id)),
                        "item_nombre": destino_doc.get('nombre') or snap.get('nombre'),
                        "sucursal_id": destino_id,
                        "cantidad": cant_recibida,
                        "stock_anterior": stock_resultante - cant_recibida,
                        "stock_resultante": stock_resultante,
                        "concepto": "TRASPASO_ENTRADA",
                        "referencia_id": traspaso_id,
                        "usuario_id": usuario_id,
                        "usuario_nombre": usuario_nombre,
                        "createdAt": datetime.utcnow(),
                    })

            # Bitácora de inventario (entrada). No es fatal si falla.
            try:
                if movimientos_entrada:
                    db["inventario_movimientos"].insert_many(movimientos_entrada)
            except Exception as bit_err:
                logger.warning(f"No se pudo registrar bitácora de traspaso (entrada): {bit_err}")

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