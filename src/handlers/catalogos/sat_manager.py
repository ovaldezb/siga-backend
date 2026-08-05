"""Catálogo SAT de claves y unidades (base `_platform`).

Permite buscar claves de producto/servicio (colección `catprodserv`) y
unidades de medida (colección `unidad`) utilizando la descripción.
"""

import json
from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.infrastructure.database import MongoDBConnection


def _platform_db():
    return MongoDBConnection.get_client()["_platform"]


def search_sat_catalogos_handler(event, context):
    """GET /catalogos/sat/{tipoBusqueda}
    Búsqueda en catálogos SAT.
    tipoBusqueda puede ser 'unidad' o 'clavesat'.
    Parámetros de consulta:
      q: término de búsqueda (descripción).
    """
    try:
        path_params = event.get('pathParameters') or {}
        tipo_busqueda = path_params.get('tipoBusqueda')

        if tipo_busqueda == 'unidad':
            collection_name = 'unidad'
        elif tipo_busqueda == 'clavesat':
            collection_name = 'catprodserv'
        else:
            return create_response(400, "Tipo de búsqueda inválido. Debe ser 'unidad' o 'clavesat'.")

        query_params = event.get('queryStringParameters') or {}
        q = (query_params.get('q') or '').strip()

        if not q or len(q) < 2:
            return create_response(200, "Búsqueda vacía", [])

        db = _platform_db()

        # Búsqueda insensible a mayúsculas/minúsculas únicamente en el campo de descripción
        query = {
            "descripcion": {"$regex": q, "$options": "i"}
        }

        cursor = db[collection_name].find(
            query, 
            {"_id": 0, "clave": 1, "descripcion": 1}
        ).limit(20)

        results = []
        for doc in cursor:
            results.append({
                "clave": doc.get("clave"),
                "descripcion": doc.get("descripcion")
            })

        return create_response(200, "Resultados de búsqueda SAT", results)

    except Exception as e:
        return handle_exception(e)
