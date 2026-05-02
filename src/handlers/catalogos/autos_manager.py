import re
from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.infrastructure.database import MongoDBConnection

def list_marcas_handler(event, context):
    """
    Obtiene la lista única de marcas de autos desde la base de datos global.
    """
    try:
        client = MongoDBConnection.get_client()
        db = client["_platform"]
        
        # Obtenemos valores únicos del campo 'marca' y los ordenamos
        marcas = sorted(db["vehiculos"].distinct("marca"))
        
        return create_response(200, "Marcas recuperadas", marcas)

    except Exception as e:
        return handle_exception(e)

def list_modelos_handler(event, context):
    """
    Obtiene la lista única de modelos para una marca específica.
    """
    try:
        query_params = event.get('queryStringParameters') or {}
        marca = query_params.get('marca')
        
        if not marca:
            return create_response(400, "El parámetro 'marca' es obligatorio")

        client = MongoDBConnection.get_client()
        db = client["_platform"]
        
        # Obtenemos modelos únicos filtrados por marca (insensible a mayúsculas)
        regex = re.compile(f"^{re.escape(marca)}$", re.IGNORECASE)
        modelos = sorted(db["vehiculos"].distinct("modelo", {"marca": regex}))
        
        return create_response(200, f"Modelos para {marca} recuperados", modelos)

    except Exception as e:
        return handle_exception(e)
