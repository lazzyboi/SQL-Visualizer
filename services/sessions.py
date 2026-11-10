import os
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import PyMongoError
from pymongo.server_api import ServerApi

from config import (
    APP_ENV,
    AUTH_DB_PATH,
    MONGO_DB,
    MONGO_SERVER_SELECTION_TIMEOUT_MS,
    MONGO_URI,
)
from services.auth import get_session_db_path, initialize_session_database
from services.storage import migrate_legacy_history, delete_history_for_session
from services.session_db_store import (
    delete_session_database,
    persist_session_database,
    restore_session_database,
)

_collection = None
_client = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect_local() -> sqlite3.Connection:
    directory = os.path.dirname(AUTH_DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(AUTH_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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
        _collection = _client[MONGO_DB]["sessions"]
        _collection.create_index([("user_id", ASCENDING), ("created_at", DESCENDING)], name="sessions_by_user_time")
        _collection.create_index([("user_id", ASCENDING), ("updated_at", DESCENDING)], name="sessions_by_user_activity")
        _collection.create_index([("id", ASCENDING), ("user_id", ASCENDING)], unique=True, name="uniq_session_id_user")
    return _collection


def init_workspace_store() -> None:
    collection = _mongo_collection()
    if collection is not None:
        try:
            if APP_ENV == "production":
                _client.admin.command("ping")
            return
        except PyMongoError as exc:
            if APP_ENV == "production":
                raise RuntimeError("MongoDB is configured but not reachable for workspace sessions.") from exc

    with _connect_local() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS workspace_sessions (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                default_session INTEGER NOT NULL DEFAULT 0,
                data_state TEXT NOT NULL DEFAULT 'seeded'
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_workspace_sessions_user ON workspace_sessions(user_id, updated_at DESC)")
        conn.commit()


def _clean(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(doc.get("id")),
        "user_id": str(doc.get("user_id")),
        "name": str(doc.get("name") or "Untitled Workspace"),
        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
        "default_session": bool(doc.get("default_session", False)),
        "data_state": str(doc.get("data_state") or "seeded"),
    }


def _path(user_id: str, session_id: str) -> str:
    return get_session_db_path(user_id, session_id)


def _ensure_db_filespace(user_id: str, workspace: dict[str, Any]) -> None:
    """Ensure the session SQLite file exists and is backed by durable storage.

    MongoDB GridFS is the source of truth whenever MONGO_URI is configured.
    The local SQLite file is only a request-time working copy on Render Free.
    Existing local files are uploaded when no remote copy exists, which also
    migrates local development databases to Atlas before deployment.
    """
    path = _path(user_id, workspace["id"])

    # Always prefer a durable remote copy. This is essential after a Render
    # restart/redeploy, where the previous instance's local filesystem is gone.
    if restore_session_database(user_id, workspace["id"], path):
        return

    # A local copy may be present during first-time migration from the current
    # developer machine. Upload it rather than replacing it with sample data.
    if os.path.exists(path) and os.path.getsize(path) > 0:
        persist_session_database(user_id, workspace["id"], path)
        return

    # The first default workspace may inherit the legacy per-user sandbox.
    if workspace.get("default_session"):
        user_dir = os.path.dirname(os.path.dirname(os.path.dirname(path)))
        legacy_path = os.path.join(user_dir, "sandbox.db")
        if os.path.exists(legacy_path) and os.path.getsize(legacy_path) > 0:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            shutil.copy2(legacy_path, path)
            persist_session_database(user_id, workspace["id"], path)
            return

    initialize_session_database(path, seed_demo=workspace.get("data_state") != "cleared")
    persist_session_database(user_id, workspace["id"], path)


def ensure_workspace_database(user_id: str, workspace: dict[str, Any]) -> str:
    """Restore or initialize one workspace database and return its local path."""
    _ensure_db_filespace(user_id, workspace)
    return _path(user_id, workspace["id"])


def list_workspaces(user_id: str) -> list[dict[str, Any]]:
    collection = _mongo_collection()
    if collection is not None:
        try:
            docs = list(collection.find({"user_id": user_id}, {"_id": 0}).sort("updated_at", DESCENDING))
            return [_clean(doc) for doc in docs]
        except Exception as exc:
            if APP_ENV == "production":
                raise RuntimeError("Workspace session storage is unavailable.") from exc

    with _connect_local() as conn:
        rows = conn.execute(
            "SELECT id, user_id, name, created_at, updated_at, default_session, data_state FROM workspace_sessions WHERE user_id = ? ORDER BY updated_at DESC",
            (user_id,),
        ).fetchall()
    return [_clean(dict(row)) for row in rows]


def get_workspace(user_id: str, session_id: str) -> dict[str, Any] | None:
    if not session_id:
        return None
    collection = _mongo_collection()
    if collection is not None:
        try:
            doc = collection.find_one({"id": session_id, "user_id": user_id}, {"_id": 0})
            return _clean(doc) if doc else None
        except Exception as exc:
            if APP_ENV == "production":
                raise RuntimeError("Workspace session lookup failed.") from exc

    with _connect_local() as conn:
        row = conn.execute(
            "SELECT id, user_id, name, created_at, updated_at, default_session, data_state FROM workspace_sessions WHERE id = ? AND user_id = ?",
            (session_id, user_id),
        ).fetchone()
    return _clean(dict(row)) if row else None


def _insert_workspace(workspace: dict[str, Any]) -> dict[str, Any]:
    collection = _mongo_collection()
    if collection is not None:
        try:
            collection.insert_one(dict(workspace))
            return _clean(workspace)
        except Exception as exc:
            if APP_ENV == "production":
                raise RuntimeError("Unable to create workspace session.") from exc

    with _connect_local() as conn:
        conn.execute(
            "INSERT INTO workspace_sessions (id, user_id, name, created_at, updated_at, default_session, data_state) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                workspace["id"],
                workspace["user_id"],
                workspace["name"],
                workspace["created_at"],
                workspace["updated_at"],
                int(workspace.get("default_session", False)),
                workspace.get("data_state", "seeded"),
            ),
        )
        conn.commit()
    return _clean(workspace)


def create_workspace(user_id: str, name: str | None = None, default_session: bool = False) -> dict[str, Any]:
    now = _now()
    clean_name = " ".join(str(name or "New Workspace").strip().split())[:80] or "New Workspace"
    workspace = {
        "id": str(uuid.uuid4()),
        "user_id": user_id,
        "name": clean_name,
        "created_at": now,
        "updated_at": now,
        "default_session": bool(default_session),
        "data_state": "seeded",
    }
    result = _insert_workspace(workspace)
    _ensure_db_filespace(user_id, result)
    return result


def ensure_default_workspace(user_id: str) -> dict[str, Any]:
    existing = list_workspaces(user_id)
    if existing:
        default_workspace = next((item for item in existing if item.get("default_session")), existing[-1])
        migrate_legacy_history(user_id, default_workspace["id"])
        # Sessions are ordered by activity; returning the most recent one gives
        # users a chat-like "continue where I left off" experience after login.
        current = existing[0]
        _ensure_db_filespace(user_id, current)
        return current

    workspace = create_workspace(user_id, "My First Workspace", default_session=True)
    migrate_legacy_history(user_id, workspace["id"])
    return workspace


def touch_workspace(user_id: str, session_id: str) -> dict[str, Any] | None:
    now = _now()
    collection = _mongo_collection()
    if collection is not None:
        try:
            collection.update_one({"id": session_id, "user_id": user_id}, {"$set": {"updated_at": now}})
            doc = collection.find_one({"id": session_id, "user_id": user_id}, {"_id": 0})
            return _clean(doc) if doc else None
        except Exception as exc:
            if APP_ENV == "production":
                raise RuntimeError("Unable to update workspace activity.") from exc

    with _connect_local() as conn:
        conn.execute(
            "UPDATE workspace_sessions SET updated_at = ? WHERE id = ? AND user_id = ?",
            (now, session_id, user_id),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id, user_id, name, created_at, updated_at, default_session, data_state FROM workspace_sessions WHERE id = ? AND user_id = ?",
            (session_id, user_id),
        ).fetchone()
    return _clean(dict(row)) if row else None


def rename_workspace(user_id: str, session_id: str, name: str) -> dict[str, Any] | None:
    clean_name = " ".join(str(name or "").strip().split())[:80]
    if not clean_name:
        raise ValueError("Workspace name cannot be empty.")
    now = _now()
    collection = _mongo_collection()
    if collection is not None:
        try:
            collection.update_one(
                {"id": session_id, "user_id": user_id},
                {"$set": {"name": clean_name, "updated_at": now}},
            )
            doc = collection.find_one({"id": session_id, "user_id": user_id}, {"_id": 0})
            return _clean(doc) if doc else None
        except Exception as exc:
            if APP_ENV == "production":
                raise RuntimeError("Unable to rename workspace.") from exc

    with _connect_local() as conn:
        conn.execute(
            "UPDATE workspace_sessions SET name = ?, updated_at = ? WHERE id = ? AND user_id = ?",
            (clean_name, now, session_id, user_id),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id, user_id, name, created_at, updated_at, default_session, data_state FROM workspace_sessions WHERE id = ? AND user_id = ?",
            (session_id, user_id),
        ).fetchone()
    return _clean(dict(row)) if row else None


def mark_workspace_cleared(user_id: str, session_id: str) -> dict[str, Any] | None:
    now = _now()
    collection = _mongo_collection()
    if collection is not None:
        try:
            collection.update_one(
                {"id": session_id, "user_id": user_id},
                {"$set": {"data_state": "cleared", "updated_at": now}},
            )
            doc = collection.find_one({"id": session_id, "user_id": user_id}, {"_id": 0})
            return _clean(doc) if doc else None
        except Exception as exc:
            if APP_ENV == "production":
                raise RuntimeError("Unable to update workspace state.") from exc

    with _connect_local() as conn:
        conn.execute(
            "UPDATE workspace_sessions SET data_state = 'cleared', updated_at = ? WHERE id = ? AND user_id = ?",
            (now, session_id, user_id),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id, user_id, name, created_at, updated_at, default_session, data_state FROM workspace_sessions WHERE id = ? AND user_id = ?",
            (session_id, user_id),
        ).fetchone()
    return _clean(dict(row)) if row else None


def delete_workspace(user_id: str, session_id: str) -> dict[str, Any]:
    workspaces = list_workspaces(user_id)
    target = next((item for item in workspaces if item["id"] == session_id), None)
    if not target:
        raise ValueError("Workspace not found.")
    if len(workspaces) <= 1:
        raise ValueError("Keep at least one workspace. Create another workspace before deleting this one.")

    collection = _mongo_collection()
    if collection is not None:
        try:
            collection.delete_one({"id": session_id, "user_id": user_id})
        except Exception as exc:
            if APP_ENV == "production":
                raise RuntimeError("Unable to delete workspace.") from exc
    else:
        with _connect_local() as conn:
            conn.execute("DELETE FROM workspace_sessions WHERE id = ? AND user_id = ?", (session_id, user_id))
            conn.commit()

    try:
        delete_history_for_session(user_id, session_id)
    except Exception:
        # Never leave a deleted workspace inaccessible solely because history cleanup
        # failed. Its database is still removed below.
        pass

    # Remove the durable SQLite file before deleting the request-time copy.
    delete_session_database(user_id, session_id)
    path = _path(user_id, session_id)
    workspace_dir = os.path.dirname(path)
    shutil.rmtree(workspace_dir, ignore_errors=True)

    remaining = list_workspaces(user_id)
    next_workspace = remaining[0]
    _ensure_db_filespace(user_id, next_workspace)
    return next_workspace
