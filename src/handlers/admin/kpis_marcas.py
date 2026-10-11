"""KPI "Mezcla de marcas y modelos" (GET /kpis/marcas-modelos?mes=AAAA-MM).

Qué marcas y modelos dejan utilidad, no solo cuáles llegan más: una marca con
muchas órdenes y poca utilidad ocupa bahías; una con pocas y mucha utilidad es
en la que conviene especializarse y tener refacciones.

El dinero sale con los mismos criterios que Reportes y Contabilidad (se reutilizan
sus funciones), para que la utilidad del mes sea el mismo número en los tres:
ventas vigentes por `createdAt`, ingreso SIN IVA y costo de `costo_unitario_snapshot`.
El vehículo es el de la OS de la venta; las ventas de mostrador no tienen y se
reportan aparte.
"""
from collections import Counter
from datetime import datetime

from src.handlers.contabilidad.contabilidad_manager import _month_range, _oid
from src.handlers.admin.reportes_periodo import (
    _cantidad, _costo_venta, _costos_pendientes, _ingreso, _lineas_con_ingreso, _num,
    _ventas_vigentes,
)

SIN_MARCA = 'Sin marca'
SIN_MODELO = 'Sin modelo'

_TOP_MODELOS_MARCA = 5
_TOP_REFACCIONES_MARCA = 5
_TOP_MODELOS = 15


def periodo_del_mes(query_params):
    """(desde, hasta_excl, ant_desde) del mes pedido como AAAA-MM; None si no es válido.
    Sin `mes`, el mes en curso."""
    mes = (query_params.get('mes') or '').strip()
    try:
        inicio = datetime.strptime(mes, '%Y-%m') if mes else datetime.utcnow()
    except ValueError:
        return None
    desde, hasta_excl = _month_range(inicio.year, inicio.month)
    anterior = (inicio.year, inicio.month - 1) if inicio.month > 1 else (inicio.year - 1, 12)
    return desde, hasta_excl, _month_range(*anterior)[0]


def _texto(valor) -> str:
    return ' '.join(str(valor or '').split())


def _vehiculos_de_ventas(db, ventas):
    """orden_id → (vehiculo_id, marca, modelo) de las OS que respaldan las ventas."""
    ids = [oid for oid in (_oid(v.get('orden_id')) for v in ventas if v.get('orden_id')) if oid]
    if not ids:
        return {}
    cursor = db.ordenes_servicio.find(
        {'_id': {'$in': ids}},
        {'vehiculo_id': 1, 'vehiculo_snapshot.marca': 1, 'vehiculo_snapshot.modelo': 1},
    )
    out = {}
    for o in cursor:
        snap = o.get('vehiculo_snapshot') or {}
        out[str(o['_id'])] = (str(o.get('vehiculo_id') or ''), _texto(snap.get('marca')), _texto(snap.get('modelo')))
    return out


class _Grupo:
    """Acumulado de una marca o de un modelo. Agrupa sin distinguir mayúsculas
    ("NISSAN" y "Nissan" son la misma) y muestra la forma más usada."""

    def __init__(self):
        self.nombres = Counter()
        self.ordenes = 0
        self.ingresos = 0.0
        self.costo = 0.0
        self.vehiculos = set()

    def sumar(self, nombre, venta, vehiculo_id):
        self.nombres[nombre] += 1
        self.ordenes += 1
        self.ingresos += _ingreso(venta)
        self.costo += _costo_venta(venta)
        if vehiculo_id:
            self.vehiculos.add(vehiculo_id)

    @property
    def nombre(self):
        return self.nombres.most_common(1)[0][0]

    @property
    def utilidad(self):
        return self.ingresos - self.costo

    def fila(self):
        return {
            'ordenes': self.ordenes,
            'vehiculos': len(self.vehiculos),
            'ingresos': round(self.ingresos, 2),
            'costo': round(self.costo, 2),
            'utilidad': round(self.utilidad, 2),
            'margen_pct': round(self.utilidad / self.ingresos * 100, 1) if self.ingresos > 0 else 0.0,
            'utilidad_por_orden': round(self.utilidad / self.ordenes, 2) if self.ordenes else 0.0,
        }


def _agrupar(db, ventas):
    """Reparte las ventas con vehículo por marca y por (marca, modelo), con las
    refacciones vendidas por marca. Devuelve además las ventas separadas en con y
    sin vehículo."""
    vehiculos = _vehiculos_de_ventas(db, ventas)
    marcas, modelos, refacciones = {}, {}, {}
    con_vehiculo, sin_vehiculo = [], []
    for v in ventas:
        vehiculo = vehiculos.get(str(v.get('orden_id') or ''))
        if not vehiculo:
            sin_vehiculo.append(v)
            continue
        con_vehiculo.append(v)
        vehiculo_id, marca, modelo = vehiculo
        marca, modelo = marca or SIN_MARCA, modelo or SIN_MODELO
        clave_marca = marca.upper()
        marcas.setdefault(clave_marca, _Grupo()).sumar(marca, v, vehiculo_id)
        modelos.setdefault((clave_marca, modelo.upper()), _Grupo()).sumar(modelo, v, vehiculo_id)

        for it, ingreso in _lineas_con_ingreso(v):
            producto = it.get('producto') or {}
            if producto.get('tipo') == 'SERVICIO':
                continue
            nombre = (producto.get('nombre') or it.get('nombre') or 'Sin nombre').strip()
            pid = producto.get('id')
            clave = (clave_marca, pid if pid and pid != 'manual' else nombre.upper())
            r = refacciones.setdefault(clave, {
                'nombre': nombre, 'no_parte': producto.get('no_parte') or '',
                'cantidad': 0.0, 'ingresos': 0.0, 'costo': 0.0,
            })
            r['cantidad'] += _cantidad(it)
            r['ingresos'] += ingreso
            r['costo'] += _cantidad(it) * _num(it.get('costo_unitario_snapshot'))
    return marcas, modelos, refacciones, con_vehiculo, sin_vehiculo


def _totales(ventas) -> dict:
    ingresos = sum(_ingreso(v) for v in ventas)
    utilidad = ingresos - sum(_costo_venta(v) for v in ventas)
    return {
        'ordenes': len(ventas),
        'ingresos': round(ingresos, 2),
        'utilidad': round(utilidad, 2),
        'margen_pct': round(utilidad / ingresos * 100, 1) if ingresos > 0 else 0.0,
    }


def _pct(parte, total) -> float:
    return round(parte / total * 100, 1) if total > 0 else 0.0


def marcas_modelos(db, desde, hasta_excl, ant_desde, sucursal_id) -> dict:
    ventas = _ventas_vigentes(db, desde, hasta_excl, sucursal_id)
    anteriores = _ventas_vigentes(db, ant_desde, desde, sucursal_id)

    marcas, modelos, refacciones, con_vehiculo, sin_vehiculo = _agrupar(db, ventas)
    marcas_ant, _, _, con_vehiculo_ant, _ = _agrupar(db, anteriores)

    total_ordenes = sum(g.ordenes for g in marcas.values())
    # La participación en utilidad se reparte sobre lo que sí ganó: una marca en
    # pérdida saldría con porcentaje negativo y las demás sumarían más de 100.
    total_utilidad = sum(max(g.utilidad, 0) for g in marcas.values())

    filas_marca = []
    for clave, g in marcas.items():
        de_la_marca = sorted(
            (m for (cm, _), m in modelos.items() if cm == clave),
            key=lambda m: m.utilidad, reverse=True,
        )
        top_refacciones = sorted(
            (r for (cm, _), r in refacciones.items() if cm == clave),
            key=lambda r: r['ingresos'], reverse=True,
        )[:_TOP_REFACCIONES_MARCA]
        anterior = marcas_ant.get(clave)
        filas_marca.append({
            'marca': g.nombre,
            **g.fila(),
            'pct_ordenes': _pct(g.ordenes, total_ordenes),
            'pct_utilidad': _pct(max(g.utilidad, 0), total_utilidad),
            'anterior': {'ordenes': anterior.ordenes, 'utilidad': round(anterior.utilidad, 2)} if anterior else None,
            'modelos': [{'modelo': m.nombre, **m.fila()} for m in de_la_marca[:_TOP_MODELOS_MARCA]],
            'modelos_total': len(de_la_marca),
            'refacciones': [{
                'nombre': r['nombre'], 'no_parte': r['no_parte'],
                'cantidad': round(r['cantidad'], 2),
                'ingresos': round(r['ingresos'], 2),
                'utilidad': round(r['ingresos'] - r['costo'], 2),
            } for r in top_refacciones],
        })
    filas_marca.sort(key=lambda f: (f['marca'] == SIN_MARCA, -f['utilidad'], -f['ordenes']))

    filas_modelo = sorted(
        ({'marca': marcas[cm].nombre, 'modelo': m.nombre, **m.fila()} for (cm, _), m in modelos.items()),
        key=lambda f: (-f['utilidad'], -f['ordenes']),
    )[:_TOP_MODELOS]

    return {
        'mes': desde.strftime('%Y-%m'),
        'mes_anterior': ant_desde.strftime('%Y-%m'),
        'resumen': {**_totales(con_vehiculo), 'marcas': len(marcas), 'modelos': len(modelos)},
        'anterior': _totales(con_vehiculo_ant),
        'sin_vehiculo': _totales(sin_vehiculo),
        'marcas': filas_marca,
        'modelos': filas_modelo,
        'costos_pendientes': _costos_pendientes(con_vehiculo),
    }
