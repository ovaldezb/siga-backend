import json
from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.infrastructure.database import MongoDBConnection

def list_modulos_handler(event, context):
    """
    Lista todos los módulos disponibles en la colección global 'modulo'.
    """
    try:
        client = MongoDBConnection.get_client()
        # Accedemos a la DB administrativa global
        db = client["_platform"]
        
        modulos = list(db["modulos"].find({}, {"_id": 0}))
        
        return create_response(200, "Módulos recuperados", modulos)

    except Exception as e:
        return handle_exception(e)
