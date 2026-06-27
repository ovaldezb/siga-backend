import os
from pymongo import MongoClient
from aws_lambda_powertools import Logger

logger = Logger()

class MongoDBConnection:
    _instance = None

    @classmethod
    def get_client(cls) -> MongoClient:
        if cls._instance is None:
            try:
                # Construct the MongoDB URI from individual components
                user = os.environ.get("MONGO_USER")
                password = os.environ.get("MONGO_PASSWORD")
                host = os.environ.get("MONGO_HOST")
                db_name = os.environ.get("MONGO_DB", "siga")

                if not (user and password and host):
                    missing = [k for k, v in {"MONGO_USER": user, "MONGO_PASSWORD": password, "MONGO_HOST": host}.items() if not v]
                    raise ValueError(f"Faltan componentes de conexión a MongoDB en variables de entorno: {', '.join(missing)}")

                mongo_uri = f"mongodb+srv://{user}:{password}@{host}/{db_name}?retryWrites=true&w=majority"

                logger.info("Creating MongoDB client...")
                # NOTA de rendimiento (cold start):
                #  - NO hacemos admin.command('ping') aquí. El ping forzaba un round-trip
                #    extra a Atlas en CADA arranque en frío. PyMongo conecta de forma
                #    perezosa en la primera operación real, así que get_client() retorna
                #    de inmediato y reutilizamos el cliente entre invocaciones (singleton).
                #  - maxPoolSize bajo: una Lambda atiende 1 request por contenedor, no
                #    necesita un pool grande.
                #  - timeouts acotados para fallar rápido en vez de colgar 29s.
                cls._instance = MongoClient(
                    mongo_uri,
                    maxPoolSize=10,
                    minPoolSize=0,
                    serverSelectionTimeoutMS=5000,
                    connectTimeoutMS=5000,
                    socketTimeoutMS=20000,
                    retryWrites=True,
                )
            except Exception as e:
                logger.error(f"Failed to create MongoDB client: {str(e)}")
                raise e
        return cls._instance

    @classmethod
    def reset(cls):
        """Cierra y descarta el cliente cacheado. Lo usa el hook de SnapStart: tras
        restaurar desde un snapshot los sockets capturados están muertos, así que
        soltamos el cliente para que la próxima operación lo recree limpio."""
        if cls._instance is not None:
            try:
                cls._instance.close()
            except Exception:
                pass
            cls._instance = None

def get_platform_db():
    client = MongoDBConnection.get_client()
    return client["_platform"]

def get_tenant_db(tenant_id: str):
    if not tenant_id:
        raise ValueError("tenant_id must be provided to get tenant database")
    client = MongoDBConnection.get_client()

    # MongoDB Atlas has a strict 38 byte limit for database names.
    # UUIDs are 36 chars. "tenant_" + 36 = 43.
    # We strip hyphens (32 chars) and use a short prefix "t_" (total 34 chars).
    safe_tenant_id = tenant_id.replace("-", "")
    return client[f"t_{safe_tenant_id}"]

# Deprecated/Legacy: keeping it temporarily for backward compatibility
# if any old code relies on it, though we should migrate to get_tenant_db.
def get_database():
    client = MongoDBConnection.get_client()
    db_name = os.environ.get("MONGO_DB", "siga")
    return client[db_name]


# --- SnapStart (opcional) ---------------------------------------------------
# Cuando se habilite Lambda SnapStart para Python, el entorno se restaura desde
# un snapshot tomado tras la fase de init. Las conexiones de red abiertas antes
# del snapshot quedan obsoletas al restaurar, por lo que reseteamos el cliente
# Mongo en el hook afterRestore. Import protegido: si el paquete no está (porque
# SnapStart no está activo) este bloque es un no-op.
try:  # pragma: no cover - solo corre en entorno Lambda con SnapStart
    from snapshot_restore_py import register_after_restore

    @register_after_restore
    def _mongo_reset_after_restore():
        logger.info("SnapStart afterRestore: reseteando cliente MongoDB.")
        MongoDBConnection.reset()
except Exception:
    pass
