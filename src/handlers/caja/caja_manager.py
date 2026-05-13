import json
from datetime import datetime
from aws_lambda_powertools import Logger
from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.infrastructure.database import get_tenant_db
from bson import ObjectId

logger = Logger()

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
            
        db = get_tenant_db(tenant_id)
        
        # Verificar si ya hay una caja abierta
        existente = db.caja_sesiones.find_one({
            "sucursal_id": sucursal_id,
            "estado": "ABIERTA"
        })
        if existente:
            return create_response(400, "Ya existe una caja abierta para esta sucursal.")
            
        nueva_sesion = {
            "sucursal_id": sucursal_id,
            "usuario_apertura_id": usuario_id,
            "usuario_apertura_nombre": usuario_nombre,
            "fecha_apertura": datetime.utcnow().isoformat() + "Z",
            "monto_inicial": monto_inicial,
            "total_ventas": 0,
            "total_entradas": 0,
            "total_salidas": 0,
            "estado": "ABIERTA",
            "movimientos": [],
            "tenant_id": tenant_id
        }
        
        result = db.caja_sesiones.insert_one(nueva_sesion)
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
        tipo = body.get('tipo') # VENTA, ENTRADA, SALIDA
        monto = float(body.get('monto', 0))
        concepto = body.get('concepto', '')
        
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")
        if not sesion_id:
            return create_response(400, "El campo 'sesion_id' es obligatorio.")
            
        db = get_tenant_db(tenant_id)
        
        movimiento = {
            "id": str(ObjectId()),
            "tipo": tipo,
            "monto": monto,
            "concepto": concepto,
            "fecha": datetime.utcnow().isoformat() + "Z",
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
        monto_final = float(body.get('monto_final', 0))
        
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")
        if not sesion_id:
            return create_response(400, "El campo 'sesion_id' es obligatorio.")
            
        db = get_tenant_db(tenant_id)
        
        cierre = {
            "estado": "CERRADA",
            "fecha_cierre": datetime.utcnow().isoformat() + "Z",
            "usuario_cierre_id": usuario_id,
            "usuario_cierre_nombre": usuario_nombre,
            "monto_final": monto_final
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
