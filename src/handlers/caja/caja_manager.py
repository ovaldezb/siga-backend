import json
from datetime import datetime
from aws_lambda_powertools import Logger
from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.infrastructure.database import get_tenant_db
from bson import ObjectId
from src.shared.utils.date_utils import iso_utc

logger = Logger()

# Set de tenants donde ya garantizamos los índices durante la vida de este container.
# Evita pegarle a Mongo en cada apertura de caja (create_index es idempotente pero
# llamarlo en cada invocación es overhead innecesario).
_ensured_indexes_tenants = set()


def _ensure_caja_indexes(db, tenant_id):
    if tenant_id in _ensured_indexes_tenants:
        return
    try:
        db.caja_sesiones.create_index(
            [("sucursal_id", 1), ("estado", 1)],
            unique=True,
            partialFilterExpression={"estado": "ABIERTA"},
            name="uniq_caja_abierta_por_sucursal"
        )
        _ensured_indexes_tenants.add(tenant_id)
    except Exception as idx_err:
        logger.warning(f"No se pudo verificar índice único de caja: {idx_err}")


def get_active_caja_handler(event, context):
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        
        query_params = event.get('queryStringParameters') or {}
        sucursal_id = query_params.get('sucursal_id') or query_params.get('sucursalId')
        
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")
        if not sucursal_id:
            return create_response(400, "El campo 'sucursal_id' es obligatorio.")
            
        db = get_tenant_db(tenant_id)
        caja = db.caja_sesiones.find_one({
            "sucursal_id": sucursal_id,
            "estado": "ABIERTA"
        })
        
        if not caja:
            return create_response(200, "No hay caja abierta", None)
            
        caja['id'] = str(caja.pop('_id'))
        return create_response(200, "Caja activa obtenida", caja)
    except Exception as e:
        return handle_exception(e)

def abrir_caja_handler(event, context):
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        usuario_id = claims.get('sub')
        usuario_nombre = claims.get('name') or claims.get('email')

        body = json.loads(event.get('body', '{}'))
        sucursal_id = body.get('sucursal_id') or body.get('sucursalId')
        monto_inicial = float(body.get('monto_inicial', 0))

        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")
        if not sucursal_id:
            return create_response(400, "El campo 'sucursal_id' es obligatorio.")
        if monto_inicial < 0:
            return create_response(400, "El monto inicial no puede ser negativo.")

        db = get_tenant_db(tenant_id)

        # Índice único parcial: previene que dos terminales abran caja simultáneamente.
        # Se cachea por tenant para no llamarlo en cada apertura.
        _ensure_caja_indexes(db, tenant_id)

        nueva_sesion = {
            "sucursal_id": sucursal_id,
            "usuario_apertura_id": usuario_id,
            "usuario_apertura_nombre": usuario_nombre,
            "fecha_apertura": iso_utc(),
            "monto_inicial": monto_inicial,
            "total_ventas": 0,
            "total_entradas": 0,
            "total_salidas": 0,
            "estado": "ABIERTA",
            "movimientos": [],
            "tenant_id": tenant_id
        }

        try:
            result = db.caja_sesiones.insert_one(nueva_sesion)
        except Exception as dup_err:
            # E11000 duplicate key ⇒ ya hay una caja abierta para esta sucursal
            if "E11000" in str(dup_err):
                return create_response(409, "Ya existe una caja abierta para esta sucursal.")
            raise
        nueva_sesion['id'] = str(result.inserted_id)
        del nueva_sesion['_id']

        return create_response(201, "Caja abierta exitosamente", nueva_sesion)
    except Exception as e:
        return handle_exception(e)

def registrar_movimiento_handler(event, context):
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        usuario_id = claims.get('sub')
        usuario_nombre = claims.get('name') or claims.get('email')

        body = json.loads(event.get('body', '{}'))
        sesion_id = body.get('sesion_id')
        tipo = (body.get('tipo') or '').upper()  # VENTA, ENTRADA, SALIDA
        try:
            monto = float(body.get('monto', 0))
        except (TypeError, ValueError):
            return create_response(400, "Monto inválido.")
        concepto = body.get('concepto', '')

        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")
        if not sesion_id:
            return create_response(400, "El campo 'sesion_id' es obligatorio.")
        if tipo not in ('VENTA', 'ENTRADA', 'SALIDA'):
            return create_response(400, "Tipo de movimiento inválido. Usa VENTA, ENTRADA o SALIDA.")
        if monto <= 0:
            return create_response(400, "El monto del movimiento debe ser mayor a cero.")

        db = get_tenant_db(tenant_id)

        movimiento = {
            "id": str(ObjectId()),
            "tipo": tipo,
            "monto": monto,
            "concepto": concepto,
            "fecha": iso_utc(),
            "usuario_id": usuario_id,
            "usuario_nombre": usuario_nombre
        }

        update_fields = {
            "$push": {"movimientos": movimiento}
        }

        if tipo == 'VENTA':
            update_fields["$inc"] = {"total_ventas": monto}
        elif tipo == 'ENTRADA':
            update_fields["$inc"] = {"total_entradas": monto}
        elif tipo == 'SALIDA':
            update_fields["$inc"] = {"total_salidas": monto}

        result = db.caja_sesiones.find_one_and_update(
            {"_id": ObjectId(sesion_id), "estado": "ABIERTA"},
            update_fields,
            return_document=True
        )

        if not result:
            return create_response(404, "Sesión de caja no encontrada o ya cerrada.")

        result['id'] = str(result.pop('_id'))
        return create_response(200, "Movimiento registrado", result)
    except Exception as e:
        return handle_exception(e)

def cerrar_caja_handler(event, context):
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        usuario_id = claims.get('sub')
        usuario_nombre = claims.get('name') or claims.get('email')

        body = json.loads(event.get('body', '{}'))
        sesion_id = body.get('sesion_id')
        try:
            monto_final = float(body.get('monto_final', 0))
        except (TypeError, ValueError):
            return create_response(400, "Monto final inválido.")
        if monto_final < 0:
            return create_response(400, "El monto final no puede ser negativo.")

        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")
        if not sesion_id:
            return create_response(400, "El campo 'sesion_id' es obligatorio.")

        db = get_tenant_db(tenant_id)

        # Calcular monto esperado en caja: inicial + entradas + ventas - salidas
        sesion = db.caja_sesiones.find_one({"_id": ObjectId(sesion_id), "estado": "ABIERTA"})
        if not sesion:
            return create_response(404, "Sesión de caja no encontrada o ya cerrada.")

        monto_esperado = (
            float(sesion.get('monto_inicial', 0))
            + float(sesion.get('total_ventas', 0))
            + float(sesion.get('total_entradas', 0))
            - float(sesion.get('total_salidas', 0))
        )
        diferencia = round(monto_final - monto_esperado, 2)

        cierre = {
            "estado": "CERRADA",
            "fecha_cierre": iso_utc(),
            "usuario_cierre_id": usuario_id,
            "usuario_cierre_nombre": usuario_nombre,
            "monto_final": monto_final,
            "monto_esperado": round(monto_esperado, 2),
            "diferencia": diferencia
        }

        result = db.caja_sesiones.find_one_and_update(
            {"_id": ObjectId(sesion_id), "estado": "ABIERTA"},
            {"$set": cierre},
            return_document=True
        )

        if not result:
            return create_response(404, "Sesión de caja no encontrada o ya cerrada.")

        result['id'] = str(result.pop('_id'))
        return create_response(200, "Caja cerrada exitosamente", result)
    except Exception as e:
        return handle_exception(e)
