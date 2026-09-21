"""Reporte del taller por periodo (GET /reportes/kpis?desde=YYYY-MM-DD&hasta=YYYY-MM-DD).

Mismos criterios que Contabilidad para que los dos módulos den el mismo número:
- el ingreso es el `total` de las ventas vigentes (operación SIN IVA);
- el costo sale de `costo_unitario_snapshot` de cada línea;
- fuera las ventas canceladas/anuladas y las que cuelgan de una OS cancelada;
- el periodo es [desde, hasta] inclusivo sobre `createdAt` de la venta.
"""
from collections import defaultdict
from datetime import datetime, timedelta

from src.handlers.contabilidad.contabilidad_manager import (
    _filtrar_ventas_vigentes, _oid, _resolve_periodo,
)
from src.shared.utils.date_utils import iso_utc

_ESTADOS_VIVOS = ['RECEPCION', 'COTIZADO', 'APROBADO', 'EN_PROCESO', 'FINALIZADO']

_PROYECCION_VENTA = {
    'cliente_id': 1, 'cliente_nombre': 1, 'items': 1, 'total': 1, 'subtotal': 1,
    'descuento': 1, 'orden_id': 1, 'createdAt': 1, 'estado': 1, 'metodo_pago': 1,
    'pagos': 1, 'saldo_pendiente': 1,
}

_TOP = 10


def _num(valor) -> float:
    try:
        return float(valor or 0)
    except (TypeError, ValueError):
        return 0.0


def _cantidad(item) -> float:
    return _num(item.get('cantidad'))


def _costo_venta(venta) -> float:
    return sum(_cantidad(it) * _num(it.get('costo_unitario_snapshot')) for it in venta.get('items') or [])


def _ingreso(venta) -> float:
    return _num(venta.get('total') or venta.get('subtotal'))


def _lineas_con_ingreso(venta):
    """Líneas de la venta con su ingreso real. El `total` ya trae el descuento
    general, así que se reparte en proporción al importe de cada línea: la suma
    de las líneas cuadra con el ingreso de la venta."""
    items = venta.get('items') or []
    importes = [_cantidad(it) * _num(it.get('precio_unitario')) for it in items]
    suma = sum(importes)
    factor = (_ingreso(venta) / suma) if suma > 0 else 0.0
    for it, importe in zip(items, importes):
        yield it, importe * factor


def _mediana(valores):
    if not valores:
        return None
    s = sorted(valores)
    mitad = len(s) // 2
    return s[mitad] if len(s) % 2 else (s[mitad - 1] + s[mitad]) / 2


def _ventas_vigentes(db, desde, hasta_excl, sucursal_id):
    filtro = {'createdAt': {'$gte': desde, '$lt': hasta_excl}}
    if sucursal_id:
        filtro['sucursal_id'] = sucursal_id
    return _filtrar_ventas_vigentes(db, list(db.ventas.find(filtro, _PROYECCION_VENTA)))


def _resumen(ventas) -> dict:
    ingresos = sum(_ingreso(v) for v in ventas)
    costo = sum(_costo_venta(v) for v in ventas)
    utilidad = ingresos - costo
    count = len(ventas)
    return {
        'ingresos': round(ingresos, 2),
        'costo_venta': round(costo, 2),
        'utilidad_bruta': round(utilidad, 2),
        'margen_pct': round(utilidad / ingresos * 100, 2) if ingresos > 0 else 0.0,
        'ventas_count': count,
        'ventas_os_count': sum(1 for v in ventas if v.get('orden_id')),
        'ticket_promedio': round(ingresos / count, 2) if count else 0.0,
        'descuentos': round(sum(_num(v.get('descuento')) for v in ventas), 2),
        'por_cobrar': round(sum(_num(v.get('saldo_pendiente')) for v in ventas), 2),
    }


def _mezcla_y_conceptos(ventas):
    """Servicios (mano de obra) contra refacciones, y lo más vendido de cada uno."""
    mezcla = {t: {'ingresos': 0.0, 'costo': 0.0, 'lineas': 0} for t in ('SERVICIO', 'REFACCION')}
    conceptos = {}
    for v in ventas:
        for it, ingreso in _lineas_con_ingreso(v):
            producto = it.get('producto') or {}
            tipo = 'SERVICIO' if producto.get('tipo') == 'SERVICIO' else 'REFACCION'
            costo = _cantidad(it) * _num(it.get('costo_unitario_snapshot'))
            m = mezcla[tipo]
            m['ingresos'] += ingreso
            m['costo'] += costo
            m['lineas'] += 1

            nombre = (producto.get('nombre') or it.get('nombre') or 'Sin nombre').strip()
            pid = producto.get('id')
            clave = (tipo, pid if pid and pid != 'manual' else nombre.upper())
            c = conceptos.setdefault(clave, {
                'nombre': nombre, 'tipo': tipo, 'no_parte': producto.get('no_parte') or '',
                'cantidad': 0.0, 'ingresos': 0.0, 'costo': 0.0, 'ventas': 0,
            })
            c['cantidad'] += _cantidad(it)
            c['ingresos'] += ingreso
            c['costo'] += costo
            c['ventas'] += 1

    for m in mezcla.values():
        m['utilidad'] = round(m['ingresos'] - m['costo'], 2)
        m['ingresos'] = round(m['ingresos'], 2)
        m['costo'] = round(m['costo'], 2)

    def _top(tipo):
        filas = [c for c in conceptos.values() if c['tipo'] == tipo]
        filas.sort(key=lambda c: c['ingresos'], reverse=True)
        return [{
            **c,
            'cantidad': round(c['cantidad'], 2),
            'ingresos': round(c['ingresos'], 2),
            'utilidad': round(c['ingresos'] - c['costo'], 2),
            'costo': round(c['costo'], 2),
        } for c in filas[:_TOP]]

    return mezcla, {'servicios': _top('SERVICIO'), 'refacciones': _top('REFACCION')}


def _top_clientes(ventas):
    clientes = {}
    for v in ventas:
        cid = v.get('cliente_id') or 'PUBLICO_GENERAL'
        c = clientes.setdefault(cid, {
            'cliente_id': cid, 'nombre': None, 'visitas': 0,
            'ingresos': 0.0, 'costo': 0.0, 'ultima_visita': None,
            'es_publico_general': cid == 'PUBLICO_GENERAL',
        })
        c['nombre'] = c['nombre'] or (v.get('cliente_nombre') or '').strip() or None
        c['visitas'] += 1
        c['ingresos'] += _ingreso(v)
        c['costo'] += _costo_venta(v)
        fecha = v.get('createdAt')
        if isinstance(fecha, datetime) and (c['ultima_visita'] is None or fecha > c['ultima_visita']):
            c['ultima_visita'] = fecha

    filas = sorted(clientes.values(), key=lambda c: c['ingresos'], reverse=True)[:_TOP]
    return [{
        **c,
        'nombre': c['nombre'] or ('Público general' if c['es_publico_general'] else 'Cliente sin nombre'),
        'ingresos': round(c['ingresos'], 2),
        'utilidad': round(c['ingresos'] - c['costo'], 2),
        'costo': round(c['costo'], 2),
        'ticket_promedio': round(c['ingresos'] / c['visitas'], 2) if c['visitas'] else 0.0,
        'ultima_visita': iso_utc(c['ultima_visita']) if c['ultima_visita'] else None,
    } for c in filas]


def _metodos_pago(ventas):
    """Cómo pagaron las ventas del periodo. El crédito aparece como método: es
    venta hecha pero dinero que todavía no entra."""
    metodos = defaultdict(float)
    for v in ventas:
        ingreso = _ingreso(v)
        pagos = [p for p in (v.get('pagos') or []) if isinstance(p, dict) and _num(p.get('monto')) > 0]
        if not pagos:
            metodos[(v.get('metodo_pago') or 'SIN REGISTRO').upper()] += ingreso
            continue
        suma = sum(_num(p.get('monto')) for p in pagos)
        # El efectivo recibido puede traer el cambio: se escala para que cuadre con la venta.
        factor = ingreso / suma if suma > ingreso > 0 else 1.0
        for p in pagos:
            metodos[str(p.get('metodo') or 'SIN REGISTRO').upper()] += _num(p.get('monto')) * factor
    total = sum(metodos.values())
    filas = [{
        'metodo': m, 'monto': round(monto, 2),
        'pct': round(monto / total * 100, 1) if total > 0 else 0.0,
    } for m, monto in metodos.items() if monto > 0]
    return sorted(filas, key=lambda f: f['monto'], reverse=True)


def _ordenes_de_ventas(db, ventas):
    ids = [oid for oid in (_oid(v.get('orden_id')) for v in ventas if v.get('orden_id')) if oid]
    if not ids:
        return {}
    return {
        str(o['_id']): o for o in db.ordenes_servicio.find(
            {'_id': {'$in': ids}},
            {'mecanico_id': 1, 'mecanico_nombre': 1, 'createdAt': 1},
        )
    }


def _mecanicos(db, ventas, sucursal_id):
    """Por mecánico: órdenes cobradas en el periodo, lo que generaron, y cuántos
    días tardaron los autos desde que entraron hasta que se cobraron."""
    ordenes = _ordenes_de_ventas(db, ventas)
    stats = {}
    estancias = []

    def _fila(mid, nombre):
        return stats.setdefault(mid, {
            'mecanico_id': mid, 'nombre': nombre, 'os_cobradas': 0,
            'ingresos': 0.0, 'costo': 0.0, '_dias': [], 'en_taller': 0,
        })

    for v in ventas:
        orden = ordenes.get(str(v.get('orden_id') or ''))
        if not orden:
            continue
        mid = orden.get('mecanico_id') or 'SIN_ASIGNAR'
        fila = _fila(mid, orden.get('mecanico_nombre') or 'Sin asignar')
        fila['os_cobradas'] += 1
        fila['ingresos'] += _ingreso(v)
        fila['costo'] += _costo_venta(v)
        entrada, cobro = orden.get('createdAt'), v.get('createdAt')
        if isinstance(entrada, datetime) and isinstance(cobro, datetime) and cobro >= entrada:
            dias = (cobro - entrada).total_seconds() / 86400
            fila['_dias'].append(dias)
            estancias.append(dias)

    # Carga actual: lo que cada quien tiene hoy en el taller (no depende del periodo).
    filtro_vivas = {'estado': {'$in': _ESTADOS_VIVOS}}
    if sucursal_id:
        filtro_vivas['sucursal_id'] = sucursal_id
    for o in db.ordenes_servicio.find(filtro_vivas, {'mecanico_id': 1, 'mecanico_nombre': 1}):
        mid = o.get('mecanico_id') or 'SIN_ASIGNAR'
        _fila(mid, o.get('mecanico_nombre') or 'Sin asignar')['en_taller'] += 1

    filas = []
    for fila in stats.values():
        dias = fila.pop('_dias')
        mediana = _mediana(dias)
        filas.append({
            **fila,
            'ingresos': round(fila['ingresos'], 2),
            'utilidad': round(fila['ingresos'] - fila['costo'], 2),
            'costo': round(fila['costo'], 2),
            'ticket_promedio': round(fila['ingresos'] / fila['os_cobradas'], 2) if fila['os_cobradas'] else 0.0,
            'dias_estancia': round(mediana, 1) if mediana is not None else None,
        })
    filas.sort(key=lambda f: (f['mecanico_id'] == 'SIN_ASIGNAR', -f['ingresos'], -f['en_taller']))
    mediana_global = _mediana(estancias)
    return filas, (round(mediana_global, 1) if mediana_global is not None else None)


def _embudo_cotizaciones(db, desde, hasta_excl, sucursal_id):
    """OS recibidas en el periodo y qué pasó con lo que se les cotizó.

    Se mide por importe y no por conteo: un cliente que aprueba el aceite y
    rechaza la suspensión es la diferencia entre una venta de $800 y una de $9,000.
    """
    filtro = {'$or': [
        {'createdAt': {'$gte': desde, '$lt': hasta_excl}},
        {'createdAt': {'$gte': desde.isoformat(), '$lt': hasta_excl.isoformat()}},
    ]}
    if sucursal_id:
        filtro['sucursal_id'] = sucursal_id
    recibidas = canceladas = 0
    montos = {'aprobado': 0.0, 'rechazado': 0.0, 'pendiente': 0.0}
    for o in db.ordenes_servicio.find(filtro, {'estado': 1, 'puntosArreglar': 1}):
        recibidas += 1
        if o.get('estado') == 'CANCELADO':
            canceladas += 1
            continue
        for punto in o.get('puntosArreglar') or []:
            for it in (punto or {}).get('items') or []:
                if not isinstance(it, dict) or it.get('no_cobrar'):
                    continue
                importe = _num(it.get('precioVenta')) * (_num(it.get('piezas')) or 1)
                if it.get('rechazado'):
                    montos['rechazado'] += importe
                elif it.get('aprobado'):
                    montos['aprobado'] += importe
                else:
                    montos['pendiente'] += importe
    decidido = montos['aprobado'] + montos['rechazado']
    return {
        'os_recibidas': recibidas,
        'os_canceladas': canceladas,
        'cotizado': round(sum(montos.values()), 2),
        **{k: round(v, 2) for k, v in montos.items()},
        'tasa_aprobacion_pct': round(montos['aprobado'] / decidido * 100, 1) if decidido > 0 else None,
    }


def _tendencia(db, hasta_excl, sucursal_id, meses=12):
    """Ingreso y utilidad de los últimos `meses` meses que terminan en el del periodo."""
    ultimo = hasta_excl - timedelta(days=1)
    indice_fin = ultimo.year * 12 + ultimo.month - 1
    buckets = []
    for i in range(meses - 1, -1, -1):
        idx = indice_fin - i
        buckets.append({'mes': f"{idx // 12:04d}-{idx % 12 + 1:02d}", 'ingresos': 0.0, 'costo': 0.0, 'ventas_count': 0})
    primer = buckets[0]['mes']
    inicio = datetime(int(primer[:4]), int(primer[5:]), 1)
    siguiente = indice_fin + 1
    fin = datetime(siguiente // 12, siguiente % 12 + 1, 1)

    por_mes = {b['mes']: b for b in buckets}
    for v in _ventas_vigentes(db, inicio, fin, sucursal_id):
        fecha = v.get('createdAt')
        if not isinstance(fecha, datetime):
            continue
        b = por_mes.get(f"{fecha.year:04d}-{fecha.month:02d}")
        if b:
            b['ingresos'] += _ingreso(v)
            b['costo'] += _costo_venta(v)
            b['ventas_count'] += 1
    return [{
        'mes': b['mes'],
        'ingresos': round(b['ingresos'], 2),
        'utilidad': round(b['ingresos'] - b['costo'], 2),
        'ventas_count': b['ventas_count'],
    } for b in buckets]


def _costos_pendientes(ventas):
    count = 0
    importe = 0.0
    for v in ventas:
        for it in v.get('items') or []:
            if it.get('costo_pendiente'):
                count += 1
                importe += _num(it.get('precio_unitario')) * (_cantidad(it) or 1)
    return {'count': count, 'importe_venta': round(importe, 2)}


def _cxc_actual(db, sucursal_id):
    """Lo que hoy le deben al taller (no depende del periodo)."""
    filtro = {'saldo_pendiente': {'$gt': 0}}
    if sucursal_id:
        filtro['sucursal_id'] = sucursal_id
    ventas = _filtrar_ventas_vigentes(db, list(db.ventas.find(filtro, {'saldo_pendiente': 1, 'estado': 1, 'orden_id': 1})))
    return {
        'total': round(sum(_num(v.get('saldo_pendiente')) for v in ventas), 2),
        'count': len(ventas),
    }


# Un reporte de más de esto recorre demasiadas ventas para una Lambda síncrona.
MAX_DIAS_PERIODO = 3 * 366


def validar_periodo(query_params):
    """Mensaje de error si el periodo no sirve; None si está bien."""
    desde, hasta_excl, _, _, es_rango = _resolve_periodo(query_params)
    if not es_rango:
        return "Las fechas 'desde' y 'hasta' deben tener el formato AAAA-MM-DD."
    if hasta_excl <= desde:
        return "La fecha 'desde' no puede ser posterior a 'hasta'."
    if (hasta_excl - desde).days > MAX_DIAS_PERIODO:
        return "El periodo no puede pasar de 3 años."
    return None


def reporte_periodo(db, query_params) -> dict:
    sucursal_id = query_params.get('sucursal_id')
    desde, hasta_excl, _, _, _ = _resolve_periodo(query_params)

    # Periodo anterior del mismo largo, inmediatamente antes: da las variaciones.
    duracion = hasta_excl - desde
    ant_desde, ant_hasta = desde - duracion, desde

    ventas = _ventas_vigentes(db, desde, hasta_excl, sucursal_id)
    anteriores = _ventas_vigentes(db, ant_desde, ant_hasta, sucursal_id)

    mezcla, conceptos = _mezcla_y_conceptos(ventas)
    mecanicos, dias_estancia = _mecanicos(db, ventas, sucursal_id)
    resumen = _resumen(ventas)
    resumen['dias_estancia_mediana'] = dias_estancia

    return {
        'desde': iso_utc(desde),
        'hasta': iso_utc(hasta_excl - timedelta(days=1)),
        'anterior': {'desde': iso_utc(ant_desde), 'hasta': iso_utc(ant_hasta - timedelta(days=1))},
        'resumen': resumen,
        'resumen_anterior': _resumen(anteriores),
        'mezcla': mezcla,
        'top_servicios': conceptos['servicios'],
        'top_refacciones': conceptos['refacciones'],
        'top_clientes': _top_clientes(ventas),
        'metodos_pago': _metodos_pago(ventas),
        'mecanicos': mecanicos,
        'cotizaciones': _embudo_cotizaciones(db, desde, hasta_excl, sucursal_id),
        'tendencia': _tendencia(db, hasta_excl, sucursal_id),
        'costos_pendientes': _costos_pendientes(ventas),
        'cuentas_por_cobrar': _cxc_actual(db, sucursal_id),
    }
