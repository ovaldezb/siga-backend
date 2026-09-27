from src.shared.utils.auth_utils import get_claims, get_groups
import json
from bson import ObjectId
from bson.errors import InvalidId
from datetime import datetime
from aws_lambda_powertools import Logger
from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.infrastructure.database import get_tenant_db
from src.shared.utils.date_utils import iso_utc

logger = Logger()

@logger.inject_lambda_context
def list_sucursales_handler(event, context):
    try:
        claims =get_claims(event)
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        db = get_tenant_db(tenant_id)
        sucursales = list(db["sucursales"].find())

        for s in sucursales:
            s['id'] = str(s.pop('_id'))
            if 'createdAt' in s and isinstance(s['createdAt'], datetime):
                s['createdAt'] = iso_utc(s['createdAt'])
            if 'updatedAt' in s and isinstance(s['updatedAt'], datetime):
                s['updatedAt'] = iso_utc(s['updatedAt'])

        return create_response(200, "Sucursales obtenidas", sucursales)
    except Exception as e:
        return handle_exception(e)

@logger.inject_lambda_context
def create_sucursal_handler(event, context):
    try:
        claims =get_claims(event)
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        body = json.loads(event.get('body', '{}'))
        db = get_tenant_db(tenant_id)

        nueva_sucursal = {
            "nombre": body.get("nombre"),
            "direccion": body.get("direccion"),
            "telefono": body.get("telefono"),
            "responsable": body.get("responsable"),
            "activa": body.get("activa", True),
            "id_certificado": body.get("id_certificado"),
            "serie": body.get("serie"),
            "regimen_fiscal": body.get("regimen_fiscal"),
            "codigo_postal": body.get("codigo_postal"),
            "codigo_sucursal": body.get("codigo_sucursal"),
            "tenant_id": tenant_id,
            "createdAt": datetime.utcnow(),
            "updatedAt": datetime.utcnow()
        }

        result = db["sucursales"].insert_one(nueva_sucursal)
        sid = str(result.inserted_id)
        
        # Inicializar contador de folios para esta sucursal
        db["folios"].insert_one({
            "tipo": "os",
            "secuencia": 0, # Empezamos en 0 para que el primer next sea 1
            "sucursal_id": sid
        })

        nueva_sucursal['id'] = sid
        del nueva_sucursal['_id']
        nueva_sucursal['createdAt'] = iso_utc(nueva_sucursal['createdAt'])
        nueva_sucursal['updatedAt'] = iso_utc(nueva_sucursal['updatedAt'])

        return create_response(201, "Sucursal creada", nueva_sucursal)
    except Exception as e:
        return handle_exception(e)

@logger.inject_lambda_context
def update_sucursal_handler(event, context):
    try:
        claims =get_claims(event)
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        sucursal_id = event['pathParameters']['id']
        body = json.loads(event.get('body', '{}'))
        db = get_tenant_db(tenant_id)

        update_data = {k: v for k, v in body.items() if k not in ['id', '_id', 'tenant_id', 'createdAt']}
        update_data['updatedAt'] = datetime.utcnow()

        result = db["sucursales"].update_one(
            {"_id": ObjectId(sucursal_id)},
            {"$set": update_data}
        )

        if result.matched_count == 0:
            return create_response(404, "Sucursal no encontrada")

        updated_sucursal = db["sucursales"].find_one({"_id": ObjectId(sucursal_id)})
        updated_sucursal['id'] = str(updated_sucursal.pop('_id'))
        if 'createdAt' in updated_sucursal and isinstance(updated_sucursal['createdAt'], datetime):
            updated_sucursal['createdAt'] = iso_utc(updated_sucursal['createdAt'])
        if 'updatedAt' in updated_sucursal and isinstance(updated_sucursal['updatedAt'], datetime):
            updated_sucursal['updatedAt'] = iso_utc(updated_sucursal['updatedAt'])

        return create_response(200, "Sucursal actualizada", updated_sucursal)
    except Exception as e:
        return handle_exception(e)

# Lo que impide borrar una sucursal: (colección, filtro, etiqueta). Cualquiera de
# estos documentos quedaría apuntando a un sucursal_id inexistente y desaparecería
# de listados, reportes y cortes filtrados por sucursal.
def _dependencias_sucursal(sid):
    return [
        ("ordenes_servicio", {"sucursal_id": sid}, "órdenes de servicio"),
        ("ventas", {"sucursal_id": sid}, "ventas"),
        ("compras", {"sucursal_id": sid}, "compras"),
        ("cotizaciones", {"sucursal_id": sid}, "cotizaciones"),
        ("citas", {"sucursal_id": sid}, "citas"),
        ("caja_sesiones", {"sucursal_id": sid}, "cortes de caja"),
        ("traspasos", {"$or": [{"origen_id": sid}, {"destino_id": sid}]}, "traspasos"),
        ("items", {"sucursal_id": sid, "stock": {"$gt": 0}}, "productos con existencias"),
        ("inventario_movimientos", {"sucursal_id": sid}, "movimientos de inventario"),
        ("gastos_fijos_mes", {"sucursal_id": sid}, "gastos fijos"),
        ("gastos_variables", {"sucursal_id": sid}, "gastos variables"),
        # El front guarda [{"sucursal": id}]; se aceptan también las formas legacy
        # que lee get_user_allowed_sucursales (id suelto, "id", "sucursal_id").
        ("usuarios", {"$or": [
            {"sucursales": sid},
            {"sucursales.sucursal": sid},
            {"sucursales.id": sid},
            {"sucursales.sucursal_id": sid},
        ]}, "usuarios asignados"),
    ]


@logger.inject_lambda_context
def delete_sucursal_handler(event, context):
    try:
        claims =get_claims(event)
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")
        if not ({'ADMIN', 'SUPER_ADMIN'} & set(get_groups(claims))):
            return create_response(403, "Solo un administrador puede eliminar sucursales.")

        sucursal_id = (event.get('pathParameters') or {}).get('id')
        try:
            oid = ObjectId(sucursal_id)
        except (InvalidId, TypeError):
            return create_response(400, "Solicitud inválida: id de sucursal inválido")

        db = get_tenant_db(tenant_id)
        if not db["sucursales"].find_one({"_id": oid}, {"_id": 1}):
            return create_response(404, "Sucursal no encontrada")
        if db["sucursales"].count_documents({}) <= 1:
            return create_response(409, "No se puede eliminar la única sucursal del taller.")

        ligados = []
        for coleccion, filtro, etiqueta in _dependencias_sucursal(sucursal_id):
            n = db[coleccion].count_documents(filtro)
            if n:
                ligados.append(f"{n} {etiqueta}")
        if ligados:
            return create_response(
                409,
                "No se puede eliminar la sucursal porque tiene " + ", ".join(ligados)
                + ". Desactívala en su lugar.",
                {"ligados": ligados},
            )

        db["sucursales"].delete_one({"_id": oid})
        # Sin historial: se limpian su contador de folios y las réplicas del
        # catálogo sin existencias que el alta de productos crea en cada sucursal.
        db["folios"].delete_many({"sucursal_id": sucursal_id})
        db["items"].delete_many({"sucursal_id": sucursal_id})

        return create_response(200, "Sucursal eliminada")
    except Exception as e:
        return handle_exception(e)


@logger.inject_lambda_context
def get_sucursal_handler(event, context):
    try:
        claims = get_claims(event)
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        sucursal_id = event['pathParameters']['id']
        db = get_tenant_db(tenant_id)
        
        sucursal = db["sucursales"].find_one({"_id": ObjectId(sucursal_id)})
        if not sucursal:
            return create_response(404, "Sucursal no encontrada")

        sucursal['id'] = str(sucursal.pop('_id'))
        if 'createdAt' in sucursal and isinstance(sucursal['createdAt'], datetime):
            sucursal['createdAt'] = iso_utc(sucursal['createdAt'])
        if 'updatedAt' in sucursal and isinstance(sucursal['updatedAt'], datetime):
            sucursal['updatedAt'] = iso_utc(sucursal['updatedAt'])

        return create_response(200, "Sucursal obtenida", sucursal)
    except Exception as e:
        return handle_exception(e)