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

def _to_money(value):
    """Castea a float redondeado a 2 decimales; devuelve None si no es numérico."""
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return None


def cerrar_caja_handler(event, context):
    """POST /caja/cierre — Cierra la sesión con arqueo (conteo físico por método).

    Acepta el desglose nuevo (`efectivo_fisico`/`tarjeta_fisico`/`otros_fisico`)
    y, por retrocompatibilidad, el `monto_final` plano de clientes viejos.
    Persiste un sub-objeto `arqueo` con el cuadre cuenta-vs-sistema para auditoría.
    """
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        usuario_id = claims.get('sub')
        usuario_nombre = claims.get('name') or claims.get('email')

        body = json.loads(event.get('body', '{}'))
        sesion_id = body.get('sesion_id')

        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")
        if not sesion_id:
            return create_response(400, "El campo 'sesion_id' es obligatorio.")

        # Arqueo: el cajero captura el conteo físico por método de pago.
        efectivo_fisico = _to_money(body.get('efectivo_fisico'))
        tarjeta_fisico = _to_money(body.get('tarjeta_fisico'))
        otros_fisico = _to_money(body.get('otros_fisico'))
        motivo = (body.get('motivo') or '').strip()

        tiene_desglose = any(x is not None for x in (efectivo_fisico, tarjeta_fisico, otros_fisico))
        if tiene_desglose:
            efectivo_fisico = efectivo_fisico or 0.0
            tarjeta_fisico = tarjeta_fisico or 0.0
            otros_fisico = otros_fisico or 0.0
            monto_final = round(efectivo_fisico + tarjeta_fisico + otros_fisico, 2)
        else:
            # Retrocompat: cliente viejo manda solo `monto_final`.
            monto_final = _to_money(body.get('monto_final'))
            if monto_final is None:
                return create_response(400, "Monto final inválido.")
            efectivo_fisico, tarjeta_fisico, otros_fisico = monto_final, 0.0, 0.0
        if monto_final < 0:
            return create_response(400, "El monto final no puede ser negativo.")

        db = get_tenant_db(tenant_id)

        sesion = db.caja_sesiones.find_one({"_id": ObjectId(sesion_id), "estado": "ABIERTA"})
        if not sesion:
            return create_response(404, "Sesión de caja no encontrada o ya cerrada.")

        # Monto esperado en caja: inicial + entradas + ventas - salidas
        monto_esperado = round(
            float(sesion.get('monto_inicial', 0))
            + float(sesion.get('total_ventas', 0))
            + float(sesion.get('total_entradas', 0))
            - float(sesion.get('total_salidas', 0)),
            2,
        )
        diferencia = round(monto_final - monto_esperado, 2)

        arqueo = {
            "efectivo_fisico": efectivo_fisico,
            "tarjeta_fisico": tarjeta_fisico,
            "otros_fisico": otros_fisico,
            "total_fisico": monto_final,
            "esperado": monto_esperado,
            "diferencia": diferencia,
            "motivo": motivo,
            "cerrado_por": usuario_nombre or usuario_id,
            "cerrado_at": iso_utc(),
        }

        cierre = {
            "estado": "CERRADA",
            "fecha_cierre": iso_utc(),
            "usuario_cierre_id": usuario_id,
            "usuario_cierre_nombre": usuario_nombre,
            "monto_final": monto_final,
            "monto_esperado": monto_esperado,
            "diferencia": diferencia,
            "arqueo": arqueo,
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


def list_arqueos_handler(event, context):
    """GET /caja/arqueos?year=&month=&sucursal_id= — Cierres de caja para auditoría.

    Devuelve las sesiones CERRADAS del periodo con su arqueo, y agrega cuántos
    días tuvieron diferencia (faltante/sobrante) para la vista de auditoría.
    """
    try:
        claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")

        qp = event.get('queryStringParameters') or {}
        sucursal_id = qp.get('sucursal_id') or qp.get('sucursalId')

        query = {"estado": "CERRADA"}
        if sucursal_id:
            query["sucursal_id"] = sucursal_id

        # Filtro mensual sobre fecha_cierre (string ISO ordenable lexicográficamente).
        try:
            year = int(qp['year'])
            month = int(qp['month'])
            inicio = datetime(year, month, 1)
            fin = datetime(year + (month // 12), (month % 12) + 1, 1)
            query["fecha_cierre"] = {"$gte": iso_utc(inicio), "$lt": iso_utc(fin)}
        except (KeyError, ValueError, TypeError):
            pass  # sin filtro de fecha: últimas 200 sesiones cerradas

        db = get_tenant_db(tenant_id)
        sesiones = list(db.caja_sesiones.find(query).sort("fecha_cierre", -1).limit(200))

        items = []
        total_diferencia = 0.0
        dias_con_diferencia = 0
        for s in sesiones:
            s['id'] = str(s.pop('_id'))
            dif = float(s.get('diferencia', 0) or 0)
            total_diferencia += dif
            if abs(dif) >= 0.01:
                dias_con_diferencia += 1
            items.append(s)

        return create_response(200, "Arqueos de caja", {
            "items": items,
            "count": len(items),
            "total_diferencia": round(total_diferencia, 2),
            "dias_con_diferencia": dias_con_diferencia,
        })
    except Exception as e:
        return handle_exception(e)
