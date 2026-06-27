"""Índices Mongo críticos por performance (item #17).

Concentra `create_index` de todas las collections con lookups/filtros frecuentes.
Cada handler de lectura llama a `ensure_indexes(db, tenant_id)` — la primera
invocación por container Lambda crea los índices (idempotente), las siguientes
hacen no-op gracias al cache en `_ensured`.

Existe un script de backfill (`scripts/ensure_indexes.py`) que recorre todos
los tenants existentes en Atlas sin esperar a que cada Lambda se calenté: útil
después de un deploy o tras agregar índices nuevos a este archivo.
"""
from aws_lambda_powertools import Logger

logger = Logger()

# Por tenant, marca si ya garantizamos índices durante la vida del container.
# `create_index` es idempotente pero pegarle a Mongo en cada invocación es overhead.
_ensured: set[str] = set()


def ensure_indexes(db, tenant_id: str) -> None:
    """Idempotente. Llamar desde handlers de lectura — se ejecuta una vez por container."""
    if tenant_id in _ensured:
        return
    try:
        # vehiculos: lookup por dueño (lista de cliente, ficha de cliente, badge).
        db.vehiculos.create_index([("cliente_id", 1)], name="vehiculos_cliente")

        # ordenes_servicio: filtros y agregaciones más frecuentes.
        db.ordenes_servicio.create_index(
            [("cliente_snapshot.id", 1), ("estado", 1)],
            name="os_cliente_estado"
        )
        db.ordenes_servicio.create_index([("estado", 1)], name="os_estado")
        db.ordenes_servicio.create_index([("vehiculo_id", 1)], name="os_vehiculo")
        db.ordenes_servicio.create_index([("sucursal_id", 1)], name="os_sucursal")

        # cotizacion_acceso: una entrada por OS; se consulta y upsert por orden_id.
        db.cotizacion_acceso.create_index(
            [("orden_id", 1)], unique=True, name="uniq_cotizacion_orden"
        )

        # citas: el listing ordena por (fecha desc, horaInicio desc, createdAt desc)
        # con scope sucursal_id; sin índice compuesto el sort cae a memoria (>10k docs).
        db.citas.create_index(
            [("sucursal_id", 1), ("fecha", -1), ("horaInicio", -1), ("createdAt", -1)],
            name="citas_scope_orden",
        )
        # Para los filtros por estado (estado / estado_in / estado_ne) antes del sort.
        db.citas.create_index(
            [("sucursal_id", 1), ("estado", 1), ("fecha", -1)],
            name="citas_scope_estado_fecha",
        )

        # clientes: filtrar por flotilla en la vista flota (item #7 audit 2026-05-17).
        db.clientes.create_index([("flotilla_id", 1)], name="clientes_flotilla")
        # clientes.telefono: consolidación automática en create_orden/create_cita busca por
        # teléfono cuando no llega clienteId (item 0 audit). Sparse para tolerar clientes sin tel.
        db.clientes.create_index([("telefono", 1)], name="clientes_telefono", sparse=True)

        # ventas: queries pesados de reportes, CxC y saldo previo del cliente en POS.
        # - sucursal_id+createdAt: reportes diarios/mensuales por sucursal (reportes_manager).
        # - cliente_id: historial cliente y agregado de saldo previo en create_venta.
        # - saldo_pendiente partial: list_cxc_handler filtra {saldo_pendiente: {$gt: 0}}.
        db.ventas.create_index([("sucursal_id", 1), ("createdAt", -1)], name="ventas_suc_fecha")
        db.ventas.create_index([("cliente_id", 1)], name="ventas_cliente")
        db.ventas.create_index(
            [("saldo_pendiente", 1)],
            name="ventas_saldo_pendiente",
            partialFilterExpression={"saldo_pendiente": {"$gt": 0}},
        )

        # compras: reportes contables y historial por proveedor.
        db.compras.create_index([("sucursal_id", 1), ("createdAt", -1)], name="compras_suc_fecha")
        db.compras.create_index([("proveedor_id", 1)], name="compras_proveedor")
        db.compras.create_index(
            [("saldo_pendiente", 1)],
            name="compras_saldo_pendiente",
            partialFilterExpression={"saldo_pendiente": {"$gt": 0}},
        )

        # gastos_fijos_mes: cierre contable mensual; query siempre por (tenant, year, month, sucursal).
        db.gastos_fijos_mes.create_index(
            [("year", 1), ("month", 1), ("sucursal_id", 1)],
            name="gastos_fijos_periodo",
        )

        _ensured.add(tenant_id)
    except Exception as e:
        logger.warning(f"No se pudo asegurar índices de tenant {tenant_id}: {e}")


def reset_cache() -> None:
    """Para tests. En producción no se llama."""
    _ensured.clear()
