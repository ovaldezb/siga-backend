from datetime import datetime


def iso_utc(dt: datetime | None = None) -> str:
    """ISO 8601 UTC con sufijo Z explícito.

    Usa esta función para todas las fechas que se escriben en Mongo o se
    devuelven en respuestas JSON, así el frontend interpreta UTC sin
    ambigüedad. Si dt es None toma datetime.utcnow().
    """
    return (dt or datetime.utcnow()).isoformat() + "Z"
