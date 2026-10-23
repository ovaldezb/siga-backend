"""KPI "Mano de obra por mecánico" (GET /kpis/mano-obra?mes=AAAA-MM).

Cuánta mano de obra factura cada mecánico. Mismos criterios de dinero que
Reportes (se reutilizan sus funciones): ventas vigentes por `createdAt`, ingreso
SIN IVA y el descuento general repartido entre las líneas. "Cobrada" es lo
facturado en la venta, esté pagada o a crédito.

Dos límites del dato, que la pantalla explica:
- Mano de obra es la línea con `producto.tipo == 'SERVICIO'`, es decir, la que
  sale de un servicio del catálogo. Una línea tecleada a mano como "MANO DE OBRA"
  queda como refacción y no suma; se cuentan aparte en `posible_mano_obra`.
- La OS tiene un solo mecánico: toda su mano de obra se le atribuye a él.
"""
from src.handlers.contabilidad.contabilidad_manager import _oid
from src.handlers.admin.reportes_periodo import _cantidad, _lineas_con_ingreso, _ventas_vigentes

SIN_ASIGNAR = 'SIN_ASIGNAR'

_TOP_SERVICIOS = 10

# Criterio estrecho a propósito: no se adivinan rectificaciones, alineaciones ni
# otros trabajos que pueden ser subcontratados y no mano de obra propia.
_PREFIJO_MANO_OBRA = 'MANO DE OBRA'


def _mecanico_id(valor) -> str:
    """Id del mecánico de la OS. Hay órdenes con el texto "null" guardado como id:
    cuentan como sin asignar."""
    mid = str(valor or '').strip()
    return SIN_ASIGNAR if mid.lower() in ('', 'null', 'undefined', 'none') else mid


def _mecanicos_de_ventas(db, ventas):
    """orden_id → (mecanico_id, nombre) de las OS que respaldan las ventas."""
    ids = [oid for oid in (_oid(v.get('orden_id')) for v in ventas if v.get('orden_id')) if oid]
    if not ids:
        return {}
    cursor = db.ordenes_servicio.find({'_id': {'$in': ids}}, {'mecanico_id': 1, 'mecanico_nombre': 1})
    out = {str(o['_id']): (_mecanico_id(o.get('mecanico_id')), o.get('mecanico_nombre') or '') for o in cursor}

    # OS viejas guardaron el mecánico sin su nombre: se completa desde `usuarios`
    # para que no aparezcan como un segundo renglón "Sin asignar".
    sin_nombre = {mid for mid, nombre in out.values() if mid != SIN_ASIGNAR and not nombre}
    if sin_nombre:
        nombres = {}
        # `mecanico_id` es el `_id` del usuario en hex.
        oids = [oid for oid in (_oid(mid) for mid in sin_nombre) if oid]
        for u in db.usuarios.find({'_id': {'$in': oids}}, {'nombre': 1, 'apellido': 1}):
            nombres[str(u['_id'])] = ' '.join(f"{u.get('nombre') or ''} {u.get('apellido') or ''}".split())
        out = {oid: (mid, nombre or nombres.get(mid) or 'Mecánico sin nombre') for oid, (mid, nombre) in out.items()}
    return out


def _nombre_linea(it) -> str:
    producto = it.get('producto') or {}
    return ' '.join((producto.get('nombre') or it.get('nombre') or 'Sin nombre').split())


def _acumular(db, ventas):
    """Reparte cada venta entre mano de obra y refacciones, por mecánico."""
    mecanicos_os = _mecanicos_de_ventas(db, ventas)
    filas, servicios = {}, {}
    posible = {'count': 0, 'importe': 0.0}
    for v in ventas:
        mid, nombre = mecanicos_os.get(str(v.get('orden_id') or ''), (SIN_ASIGNAR, ''))
        fila = filas.setdefault(mid, {
            'nombre': 'Sin asignar', 'ordenes': 0, 'ordenes_con_mano_obra': 0,
            'mano_obra': 0.0, 'refacciones': 0.0,
        })
        if nombre and mid != SIN_ASIGNAR:
            fila['nombre'] = nombre
        fila['ordenes'] += 1

        mano_obra_venta = 0.0
        for it, ingreso in _lineas_con_ingreso(v):
            nombre_linea = _nombre_linea(it)
            if (it.get('producto') or {}).get('tipo') == 'SERVICIO':
                mano_obra_venta += ingreso
                s = servicios.setdefault(nombre_linea.upper(), {'nombre': nombre_linea, 'cantidad': 0.0, 'ingresos': 0.0, 'lineas': 0})
                s['cantidad'] += _cantidad(it)
                s['ingresos'] += ingreso
                s['lineas'] += 1
            else:
                fila['refacciones'] += ingreso
                if nombre_linea.upper().startswith(_PREFIJO_MANO_OBRA):
                    posible['count'] += 1
                    posible['importe'] += ingreso
        fila['mano_obra'] += mano_obra_venta
        if mano_obra_venta > 0:
            fila['ordenes_con_mano_obra'] += 1
    return filas, servicios, posible


def _pct(parte, total) -> float:
    return round(parte / total * 100, 1) if total > 0 else 0.0


def _totales(filas) -> dict:
    mano_obra = sum(f['mano_obra'] for f in filas.values())
    refacciones = sum(f['refacciones'] for f in filas.values())
    con_mano_obra = sum(f['ordenes_con_mano_obra'] for f in filas.values())
    return {
        'mano_obra': round(mano_obra, 2),
        'refacciones': round(refacciones, 2),
        'ingresos': round(mano_obra + refacciones, 2),
        'pct_mano_obra': _pct(mano_obra, mano_obra + refacciones),
        'ordenes': sum(f['ordenes'] for f in filas.values()),
        'ordenes_con_mano_obra': con_mano_obra,
        'promedio_por_orden': round(mano_obra / con_mano_obra, 2) if con_mano_obra else 0.0,
    }


def mano_obra(db, desde, hasta_excl, ant_desde, sucursal_id) -> dict:
    filas, servicios, posible = _acumular(db, _ventas_vigentes(db, desde, hasta_excl, sucursal_id))
    filas_ant, _, _ = _acumular(db, _ventas_vigentes(db, ant_desde, desde, sucursal_id))

    resumen = _totales(filas)
    total_mano_obra = resumen['mano_obra']

    mecanicos = []
    for mid, f in filas.items():
        anterior = filas_ant.get(mid)
        mecanicos.append({
            'mecanico_id': None if mid == SIN_ASIGNAR else mid,
            'nombre': f['nombre'],
            'ordenes': f['ordenes'],
            'ordenes_con_mano_obra': f['ordenes_con_mano_obra'],
            'mano_obra': round(f['mano_obra'], 2),
            'refacciones': round(f['refacciones'], 2),
            'pct_del_taller': _pct(f['mano_obra'], total_mano_obra),
            'promedio_por_orden': round(f['mano_obra'] / f['ordenes_con_mano_obra'], 2) if f['ordenes_con_mano_obra'] else 0.0,
            'mano_obra_anterior': round(anterior['mano_obra'], 2) if anterior else None,
        })
    mecanicos.sort(key=lambda m: (m['mecanico_id'] is None, -m['mano_obra'], -m['ordenes']))

    top_servicios = sorted(servicios.values(), key=lambda s: s['ingresos'], reverse=True)[:_TOP_SERVICIOS]

    return {
        'mes': desde.strftime('%Y-%m'),
        'mes_anterior': ant_desde.strftime('%Y-%m'),
        'resumen': {**resumen, 'mecanicos': sum(1 for m in mecanicos if m['mecanico_id'] and m['mano_obra'] > 0)},
        'anterior': _totales(filas_ant),
        'mecanicos': mecanicos,
        'servicios': [{
            'nombre': s['nombre'], 'cantidad': round(s['cantidad'], 2),
            'ingresos': round(s['ingresos'], 2), 'lineas': s['lineas'],
        } for s in top_servicios],
        'posible_mano_obra': {'count': posible['count'], 'importe': round(posible['importe'], 2)},
    }
