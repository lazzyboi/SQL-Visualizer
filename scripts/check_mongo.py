from pathlib import Path
import os
from urllib.parse import urlparse

from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.server_api import ServerApi

BASE_DIR = Path(__file__).resolve().parents[1]
load_dotenv(BASE_DIR / ".env")

uri = os.getenv("MONGO_URI", "").strip()
if not uri:
    raise SystemExit("MONGO_URI is not configured in .env")

parsed = urlparse(uri)
print(f"Cluster host : {parsed.hostname or 'unknown'}")
print(f"Mongo user   : {parsed.username or 'unknown'}")
print(f"Password     : {'configured' if parsed.password else 'not present'}")
print(f"Database     : {os.getenv('MONGO_DB', 'sqlviz')}")

client = MongoClient(uri, serverSelectionTimeoutMS=5000, connectTimeoutMS=5000, server_api=ServerApi("1"))
client.admin.command("ping")
print("Ping         : OK")

db_name = os.getenv("MONGO_DB", "sqlviz")
db = client[db_name]
for name in sorted(db.list_collection_names()):
    if name.startswith("system."):
        continue
    print(f"Collection   : {name} ({db[name].estimated_document_count()} document(s))")

