import os
from pymongo import MongoClient
from bson import ObjectId
import re
from dotenv import load_dotenv

load_dotenv()

MONGO_USER = os.environ.get("MONGO_USER")
MONGO_PASSWORD = os.environ.get("MONGO_PASSWORD")
MONGO_HOST = os.environ.get("MONGO_HOST")
MONGO_DB = os.environ.get("MONGO_DB", "siga")

# Regex para UUID
UUID_REGEX = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$', re.IGNORECASE)

def check_uuids():
    uri = f"mongodb+srv://{MONGO_USER}:{MONGO_PASSWORD}@{MONGO_HOST}/{MONGO_DB}?retryWrites=true&w=majority"
    client = MongoClient(uri)
    
    dbs = [d for d in client.list_database_names() if d.startswith("t_")]
    
    for db_name in dbs:
        db = client[db_name]
        print(f"--- Checking DB: {db_name} ---")
        
        # Check items collection
        items = list(db.items.find())
        for item in items:
            for key, value in item.items():
                if isinstance(value, str) and UUID_REGEX.match(value):
                    if key == "tenant_id":
                        continue # Tenant ID as UUID is expected
                    print(f"Found UUID in item {item['_id']} field '{key}': {value}")

if __name__ == "__main__":
    check_uuids()
