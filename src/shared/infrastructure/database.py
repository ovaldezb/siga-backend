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
                # Prioritize MONGO_URI from env
                mongo_uri = os.environ.get("MONGO_URI")
                
                # If not found, try to construct it from individual components
                if not mongo_uri:
                    user = os.environ.get("MONGO_USER")
                    password = os.environ.get("MONGO_PASSWORD")
                    host = os.environ.get("MONGO_HOST")
                    db_name = os.environ.get("MONGO_DB", "siga")
                    
                    if user and password and host:
                        mongo_uri = f"mongodb+srv://{user}:{password}@{host}/{db_name}?retryWrites=true&w=majority"
                    else:
                        raise ValueError("No MongoDB connection string or components found in environment variables.")

                logger.info("Connecting to MongoDB Atlas...")
                cls._instance = MongoClient(mongo_uri)
                # Force a connection to verify
                cls._instance.admin.command('ping')
                logger.info("Successfully connected to MongoDB Atlas")
            except Exception as e:
                logger.error(f"Failed to connect to MongoDB: {str(e)}")
                raise e
        return cls._instance

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
