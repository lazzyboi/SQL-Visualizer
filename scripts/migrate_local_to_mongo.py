"""Import local Phase 2 SQLite users and JSON history into MongoDB.

Run this once before moving an existing local development instance to Atlas.
The script never prints password hashes or the MongoDB password.
"""
from pathlib import Path
import json
import os
import sqlite3

from dotenv import load_dotenv
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.server_api import ServerApi

BASE_DIR = Path(__file__).resolve().parents[1]
load_dotenv(BASE_DIR / ".env")

uri = os.getenv("MONGO_URI", "").strip()
if not uri:
    raise SystemExit("MONGO_URI is not configured in .env")

mongo_db = os.getenv("MONGO_DB", "sqlviz")
users_collection = os.getenv("MONGO_USERS_COLLECTION", "users")
history_collection = os.getenv("MONGO_HISTORY_COLLECTION", "query_history")
auth_db_path = os.getenv("AUTH_DB_PATH", str(BASE_DIR / "data" / "app.db"))
history_file = os.getenv("HISTORY_FILE", str(BASE_DIR / "data" / "history.json"))

client = MongoClient(uri, serverSelectionTimeoutMS=5000, connectTimeoutMS=5000, server_api=ServerApi("1"))
client.admin.command("ping")
db = client[mongo_db]
users = db[users_collection]
queries = db[history_collection]
users.create_index([("email", ASCENDING)], unique=True, name="uniq_email")
users.create_index([("id", ASCENDING)], unique=True, name="uniq_user_id")
queries.create_index([("user_id", ASCENDING), ("created_at", DESCENDING)], name="history_by_user_time")

migrated_users = 0
if Path(auth_db_path).exists():
    with sqlite3.connect(auth_db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT id, email, password_hash, role, created_at FROM users").fetchall()
    for row in rows:
        result = users.update_one(
            {"id": row["id"]},
            {"$setOnInsert": dict(row)},
            upsert=True,
        )
        migrated_users += 1 if result.upserted_id is not None else 0

migrated_queries = 0
if Path(history_file).exists():
    try:
        history = json.loads(Path(history_file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        history = []
    for item in history if isinstance(history, list) else []:
        item = dict(item)
        item.pop("_id", None)
        if not item.get("id") or not item.get("user_id"):
            continue
        result = queries.update_one(
            {"id": item["id"], "user_id": item["user_id"]},
            {"$setOnInsert": item},
            upsert=True,
        )
        migrated_queries += 1 if result.upserted_id is not None else 0

print(f"MongoDB ping: OK")
print(f"Users available locally: {len(rows) if Path(auth_db_path).exists() else 0}")
print(f"Users imported: {migrated_users}")
print(f"History imported: {migrated_queries}")
print(f"Database: {mongo_db}")
print(f"Users collection: {users_collection}")
print(f"History collection: {history_collection}")

