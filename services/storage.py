import json
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import PyMongoError
from pymongo.server_api import ServerApi

from config import (
    APP_ENV,
    HISTORY_FILE,
    MAX_HISTORY,
    MONGO_DB,
    MONGO_HISTORY_COLLECTION,
    MONGO_SERVER_SELECTION_TIMEOUT_MS,
    MONGO_URI,
)

_lock = threading.Lock()
_client = None
_collection = None


class StorageUnavailable(RuntimeError):
    pass


def _mongo_collection():
    global _client, _collection
    if not MONGO_URI:
        return None
    if _collection is None:
        _client = MongoClient(
            MONGO_URI,
            serverSelectionTimeoutMS=MONGO_SERVER_SELECTION_TIMEOUT_MS,
            connectTimeoutMS=MONGO_SERVER_SELECTION_TIMEOUT_MS,
            tz_aware=True,
            server_api=ServerApi("1"),
        )
        _collection = _client[MONGO_DB][MONGO_HISTORY_COLLECTION]
        _collection.create_index(
            [("id", ASCENDING), ("user_id", ASCENDING)],
            unique=True,
            name="uniq_history_id_user",
        )
        _collection.create_index(
            [("user_id", ASCENDING), ("created_at", DESCENDING)],
            name="history_by_user_time",
        )
        _collection.create_index(
            [("user_id", ASCENDING), ("session_id", ASCENDING), ("created_at", DESCENDING)],
            name="history_by_user_session_time",
        )
    return _collection


def mongo_status() -> dict[str, Any]:
    if not MONGO_URI:
        return {"configured": False, "connected": False, "message": "MongoDB is not configured."}
    try:
        collection = _mongo_collection()
        _client.admin.command("ping")
        return {
            "configured": True,
            "connected": True,
            "database": MONGO_DB,
            "history_collection": collection.name,
            "message": "MongoDB connection is healthy.",
        }
    except Exception as exc:
        return {
            "configured": True,
            "connected": False,
            "database": MONGO_DB,
            "history_collection": MONGO_HISTORY_COLLECTION,
            "message": f"MongoDB connection failed: {type(exc).__name__}",
        }


def mongo_collections() -> list[dict[str, Any]]:
    if not MONGO_URI:
        return []
    try:
        db = _client[MONGO_DB]
        names = [name for name in db.list_collection_names() if not name.startswith("system.")]
        return [{"name": name, "count": db[name].estimated_document_count()} for name in sorted(names)]
    except Exception as exc:
        if APP_ENV == "production":
            raise StorageUnavailable("MongoDB metadata is unavailable.") from exc
        return []


def _read_local() -> list[dict[str, Any]]:
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _write_local(data: list[dict[str, Any]]) -> None:
    directory = os.path.dirname(HISTORY_FILE)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(HISTORY_FILE, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)


def _history_filter(user_id: str, session_id: str | None = None) -> dict[str, Any]:
    query: dict[str, Any] = {"user_id": user_id}
    if session_id:
        query["session_id"] = session_id
    return query


def create_history(
    user_id: str,
    result: dict[str, Any],
    session_id: str | None = None,
) -> dict[str, Any]:
    item = dict(result)
    item["id"] = str(uuid.uuid4())
    item["user_id"] = user_id
    if session_id:
        item["session_id"] = session_id
    item["created_at"] = datetime.now(timezone.utc).isoformat()

    collection = _mongo_collection()
    if collection is not None:
        try:
            collection.insert_one(item.copy())
            return item
        except Exception as exc:
            if APP_ENV == "production":
                raise StorageUnavailable("MongoDB history write failed.") from exc

    with _lock:
        data = _read_local()
        data.insert(0, item)
        # Keep the most recent MAX_HISTORY records per user/session. A busy
        # workspace must never evict another workspace's query history.
        retained: list[dict[str, Any]] = []
        counts: dict[tuple[str, str], int] = {}
        for record in data:
            scope = (str(record.get("user_id") or ""), str(record.get("session_id") or "__legacy__"))
            count = counts.get(scope, 0)
            if count >= MAX_HISTORY:
                continue
            retained.append(record)
            counts[scope] = count + 1
        _write_local(retained)
    return item


def list_history(
    user_id: str,
    limit: int = 100,
    session_id: str | None = None,
) -> list[dict[str, Any]]:
    collection = _mongo_collection()
    query = _history_filter(user_id, session_id)
    if collection is not None:
        try:
            return list(collection.find(query, {"_id": 0}).sort("created_at", -1).limit(limit))
        except Exception as exc:
            if APP_ENV == "production":
                raise StorageUnavailable("MongoDB history read failed.") from exc

    with _lock:
        data = [item for item in _read_local() if item.get("user_id") == user_id]
        if session_id:
            data = [item for item in data if item.get("session_id") == session_id]
        return data[:limit]


def get_history(
    user_id: str,
    record_id: str,
    session_id: str | None = None,
) -> dict[str, Any] | None:
    collection = _mongo_collection()
    query = {"id": record_id, "user_id": user_id}
    if session_id:
        query["session_id"] = session_id
    if collection is not None:
        try:
            return collection.find_one(query, {"_id": 0})
        except Exception as exc:
            if APP_ENV == "production":
                raise StorageUnavailable("MongoDB history read failed.") from exc

    with _lock:
        for item in _read_local():
            if item.get("id") != record_id or item.get("user_id") != user_id:
                continue
            if session_id and item.get("session_id") != session_id:
                continue
            return item
    return None


def update_history(
    user_id: str,
    record_id: str,
    updates: dict[str, Any],
    session_id: str | None = None,
) -> dict[str, Any] | None:
    collection = _mongo_collection()
    query = {"id": record_id, "user_id": user_id}
    if session_id:
        query["session_id"] = session_id
    if collection is not None:
        try:
            collection.update_one(query, {"$set": updates})
            return collection.find_one(query, {"_id": 0})
        except Exception as exc:
            if APP_ENV == "production":
                raise StorageUnavailable("MongoDB history update failed.") from exc

    with _lock:
        data = _read_local()
        for item in data:
            if item.get("id") != record_id or item.get("user_id") != user_id:
                continue
            if session_id and item.get("session_id") != session_id:
                continue
            item.update(updates)
            _write_local(data)
            return item
    return None


def migrate_legacy_history(user_id: str, session_id: str) -> int:
    """Attach old per-user history records to the user's first workspace once."""
    migrated = 0
    collection = _mongo_collection()
    if collection is not None:
        try:
            result = collection.update_many(
                {
                    "user_id": user_id,
                    "$or": [
                        {"session_id": {"$exists": False}},
                        {"session_id": None},
                        {"session_id": ""},
                    ],
                },
                {"$set": {"session_id": session_id}},
            )
            migrated += int(result.modified_count)
            return migrated
        except Exception as exc:
            if APP_ENV == "production":
                raise StorageUnavailable("MongoDB legacy history migration failed.") from exc

    with _lock:
        data = _read_local()
        changed = False
        for item in data:
            if item.get("user_id") != user_id:
                continue
            if item.get("session_id"):
                continue
            item["session_id"] = session_id
            migrated += 1
            changed = True
        if changed:
            _write_local(data)
    return migrated


def delete_history_for_session(user_id: str, session_id: str) -> int:
    """Delete all query history belonging to one workspace/session."""
    collection = _mongo_collection()
    if collection is not None:
        try:
            result = collection.delete_many({"user_id": user_id, "session_id": session_id})
            return int(result.deleted_count)
        except Exception as exc:
            if APP_ENV == "production":
                raise StorageUnavailable("MongoDB workspace history deletion failed.") from exc

    with _lock:
        data = _read_local()
        kept = [
            item
            for item in data
            if not (item.get("user_id") == user_id and item.get("session_id") == session_id)
        ]
        deleted = len(data) - len(kept)
        if deleted:
            _write_local(kept)
        return deleted


def admin_list_queries(limit: int = 100) -> list[dict[str, Any]]:
    collection = _mongo_collection()
    if collection is not None:
        try:
            return list(collection.find({}, {"_id": 0}).sort("created_at", -1).limit(limit))
        except Exception as exc:
            if APP_ENV == "production":
                raise StorageUnavailable("MongoDB query history is unavailable.") from exc
    with _lock:
        return _read_local()[:limit]
