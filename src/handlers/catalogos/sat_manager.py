"""Catálogo SAT de claves y unidades (base `_platform`).

Permite buscar claves de producto/servicio (colección `catprodserv`) y
unidades de medida (colección `unidad`) utilizando la descripción.
"""

import json
from aws_lambda_powertools import Logger
from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.infrastructure.database import MongoDBConnection

logger = Logger()


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
        elif tipo_busqueda == 'regimenfiscal':
            collection_name = 'regimen_fiscal'
        else:
            return create_response(400, "Tipo de búsqueda inválido. Debe ser 'unidad', 'clavesat' o 'regimenfiscal'.")

        query_params = event.get('queryStringParameters') or {}
        q = (query_params.get('q') or '').strip()

        logger.info(f"SAT Search query: q='{q}', tipo_busqueda='{tipo_busqueda}'")

        if tipo_busqueda != 'regimenfiscal' and (not q or len(q) < 2):
            return create_response(200, "Búsqueda vacía", [])

        db = _platform_db()

        if tipo_busqueda == 'regimenfiscal':
            query = {}
            if q:
                query["descripcion"] = {"$regex": q, "$options": "i"}
            cursor = db[collection_name].find(
                query,
                {"_id": 0, "regimenfiscal": 1, "descripcion": 1, "fisica": 1, "moral": 1}
            ).limit(100)
        else:
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
            if tipo_busqueda == 'regimenfiscal':
                results.append({
                    "clave": doc.get("regimenfiscal"),
                    "descripcion": doc.get("descripcion"),
                    "fisica": doc.get("fisica"),
                    "moral": doc.get("moral")
                })
            else:
                results.append({
                    "clave": doc.get("clave"),
                    "descripcion": doc.get("descripcion")
                })

        logger.info(f"SAT Search returned {len(results)} results")

        return create_response(200, "Resultados de búsqueda SAT", results)

    except Exception as e:
        return handle_exception(e)
