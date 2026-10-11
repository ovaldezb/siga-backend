from aws_lambda_powertools import Logger
from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.utils.auth_utils import get_claims, es_mecanico
from src.shared.infrastructure.database import get_tenant_db
from src.handlers.admin.kpis_marcas import marcas_modelos, periodo_del_mes
from src.handlers.admin.kpis_mano_obra import mano_obra

logger = Logger()


def _kpi_del_mes(event, calcular, mensaje):
    """Valida al usuario y el mes, y responde `calcular(db, desde, hasta_excl, ant_desde, sucursal_id)`."""
    try:
        claims = get_claims(event)
        tenant_id = claims.get('custom:tenant_id')
        if not tenant_id:
            return create_response(403, "No autorizado")
        if es_mecanico(claims):
            return create_response(403, "Tu usuario no tiene acceso a los indicadores del taller.")

        query_params = event.get('queryStringParameters') or {}
        periodo = periodo_del_mes(query_params)
        if not periodo:
            return create_response(400, "El mes debe tener el formato AAAA-MM.")

        db = get_tenant_db(tenant_id)
        return create_response(200, mensaje, calcular(db, *periodo, query_params.get('sucursal_id')))
    except Exception as e:
        return handle_exception(e, event)


@logger.inject_lambda_context
def get_marcas_modelos_handler(event, context):
    """GET /kpis/marcas-modelos?mes=AAAA-MM&sucursal_id= — utilidad por marca y modelo."""
    return _kpi_del_mes(event, marcas_modelos, "Mezcla de marcas y modelos generada")


@logger.inject_lambda_context
def get_mano_obra_handler(event, context):
    """GET /kpis/mano-obra?mes=AAAA-MM&sucursal_id= — mano de obra facturada por mecánico."""
    return _kpi_del_mes(event, mano_obra, "Mano de obra por mecánico generada")
