"""Persist per-session SQLite database files in MongoDB Atlas GridFS.

The application continues to query a normal SQLite working file. On Render
Free that file is only a cache: durable copies live in GridFS and are restored
before database-backed requests, then changed copies are saved afterward.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any

from gridfs import GridFSBucket
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.server_api import ServerApi

from config import APP_ENV, MONGO_DB, MONGO_SERVER_SELECTION_TIMEOUT_MS, MONGO_URI

logger = logging.getLogger(__name__)
_BUCKET_NAME = os.getenv("MONGO_SESSION_DB_BUCKET", "session_databases").strip() or "session_databases"
_client: MongoClient | None = None
_db = None
_bucket: GridFSBucket | None = None
_init_lock = threading.RLock()
_write_lock = threading.RLock()
_lock_registry_guard = threading.Lock()
_session_locks: dict[str, threading.RLock] = {}


class SessionDatabaseStoreError(RuntimeError):
    """Raised when durable session database storage is unavailable."""


def _safe_identifier(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "", str(value or ""))
    if not cleaned:
        raise ValueError("Invalid session database identifier")
    return cleaned


def acquire_session_database_lock(user_id: str, session_id: str) -> threading.RLock:
    """Serialize restore/use/save requests for one session in this worker."""
    key = f"{_safe_identifier(user_id)}:{_safe_identifier(session_id)}"
    with _lock_registry_guard:
        lock = _session_locks.setdefault(key, threading.RLock())
    lock.acquire()
    return lock


def _get_bucket() -> GridFSBucket | None:
    global _client, _db, _bucket
    if not MONGO_URI:
        if APP_ENV == "production":
            raise SessionDatabaseStoreError("MONGO_URI is required for persistent session databases.")
        return None
    if _bucket is not None:
        return _bucket
    with _init_lock:
        if _bucket is not None:
            return _bucket
        try:
            _client = MongoClient(
                MONGO_URI,
                serverSelectionTimeoutMS=MONGO_SERVER_SELECTION_TIMEOUT_MS,
                connectTimeoutMS=MONGO_SERVER_SELECTION_TIMEOUT_MS,
                tz_aware=True,
                server_api=ServerApi("1"),
            )
            _client.admin.command("ping")
            _db = _client[MONGO_DB]
            _bucket = GridFSBucket(_db, bucket_name=_BUCKET_NAME)
            _db[f"{_BUCKET_NAME}.files"].create_index(
                [
                    ("metadata.user_id", ASCENDING),
                    ("metadata.session_id", ASCENDING),
                    ("uploadDate", DESCENDING),
                ],
                name="session_database_lookup",
            )
            return _bucket
        except Exception as exc:
            _client = None
            _db = None
            _bucket = None
            if APP_ENV == "production":
                raise SessionDatabaseStoreError("MongoDB Atlas is unavailable for session database storage.") from exc
            logger.warning("MongoDB session database storage unavailable; using local SQLite files: %s", type(exc).__name__)
            return None


def _file_query(user_id: str, session_id: str) -> dict[str, Any]:
    return {
        "metadata.user_id": _safe_identifier(user_id),
        "metadata.session_id": _safe_identifier(session_id),
    }


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_is_valid(path: str) -> bool:
    try:
        uri = f"file:{Path(path).resolve().as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=10)
        try:
            row = conn.execute("PRAGMA quick_check").fetchone()
            return bool(row and row[0] == "ok")
        finally:
            conn.close()
    except (OSError, sqlite3.DatabaseError, sqlite3.Error):
        return False


def restore_session_database(user_id: str, session_id: str, path: str) -> bool:
    """Restore the newest durable session database into the local working path."""
    bucket = _get_bucket()
    if bucket is None:
        return False

    query = _file_query(user_id, session_id)
    files = _db[f"{_BUCKET_NAME}.files"]
    try:
        remote = files.find_one(query, sort=[("uploadDate", DESCENDING)])
        if remote is None:
            return False

        remote_hash = (remote.get("metadata") or {}).get("sha256")
        if os.path.isfile(path) and remote_hash and _sha256(path) == remote_hash:
            return True

        os.makedirs(os.path.dirname(path), exist_ok=True)
        temporary_path = f"{path}.restore-tmp"
        try:
            stream = bucket.open_download_stream(remote["_id"])
            try:
                with open(temporary_path, "wb") as output:
                    while True:
                        chunk = stream.read(1024 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
            finally:
                stream.close()

            downloaded_hash = _sha256(temporary_path)
            if remote_hash and downloaded_hash != remote_hash:
                raise SessionDatabaseStoreError("Downloaded session database failed its checksum verification.")
            if not _sqlite_is_valid(temporary_path):
                raise SessionDatabaseStoreError("Stored session database is not a valid SQLite database.")
            os.replace(temporary_path, path)
            return True
        finally:
            if os.path.exists(temporary_path):
                os.remove(temporary_path)
    except SessionDatabaseStoreError:
        raise
    except Exception as exc:
        if APP_ENV == "production":
            raise SessionDatabaseStoreError("Unable to restore this session database from MongoDB Atlas.") from exc
        logger.warning("Unable to restore session database; using local file: %s", type(exc).__name__)
        return False


def persist_session_database(user_id: str, session_id: str, path: str) -> bool:
    """Upload a changed valid SQLite file to GridFS; unchanged files are skipped."""
    bucket = _get_bucket()
    if bucket is None:
        return False
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        return False
    if not _sqlite_is_valid(path):
        raise SessionDatabaseStoreError("Refusing to persist an invalid SQLite session database.")

    clean_user = _safe_identifier(user_id)
    clean_session = _safe_identifier(session_id)
    query = _file_query(clean_user, clean_session)
    digest = _sha256(path)
    files = _db[f"{_BUCKET_NAME}.files"]
    filename = f"{clean_user}__{clean_session}.sqlite3"

    with _write_lock:
        try:
            latest = files.find_one(query, sort=[("uploadDate", DESCENDING)])
            latest_hash = ((latest or {}).get("metadata") or {}).get("sha256")
            if latest is not None and latest_hash == digest:
                stale = files.find({**query, "_id": {"$ne": latest["_id"]}}, {"_id": 1})
                for item in stale:
                    bucket.delete(item["_id"])
                return False

            metadata = {
                "user_id": clean_user,
                "session_id": clean_session,
                "sha256": digest,
                "size_bytes": os.path.getsize(path),
                "format": "sqlite3",
            }
            with open(path, "rb") as source:
                new_id = bucket.upload_from_stream(filename, source, metadata=metadata)

            # The new complete version exists before previous copies are removed.
            stale = files.find({**query, "_id": {"$ne": new_id}}, {"_id": 1})
            for item in stale:
                bucket.delete(item["_id"])
            return True
        except Exception as exc:
            if APP_ENV == "production":
                raise SessionDatabaseStoreError("Unable to save this session database to MongoDB Atlas.") from exc
            logger.warning("Unable to persist session database; local file remains available: %s", type(exc).__name__)
            return False


def delete_session_database(user_id: str, session_id: str) -> int:
    """Delete all GridFS revisions belonging to one session."""
    bucket = _get_bucket()
    if bucket is None:
        return 0
    query = _file_query(user_id, session_id)
    files = _db[f"{_BUCKET_NAME}.files"]
    try:
        ids = [item["_id"] for item in files.find(query, {"_id": 1})]
        for file_id in ids:
            bucket.delete(file_id)
        return len(ids)
    except Exception as exc:
        if APP_ENV == "production":
            raise SessionDatabaseStoreError("Unable to delete this session database from MongoDB Atlas.") from exc
        logger.warning("Unable to delete remote session database: %s", type(exc).__name__)
        return 0


def session_database_store_status() -> dict[str, Any]:
    if not MONGO_URI:
        return {
            "configured": False,
            "connected": False,
            "bucket": _BUCKET_NAME,
            "message": "Session databases are stored locally; MongoDB persistence is not configured.",
        }
    try:
        _get_bucket()
        if _client is None:
            raise SessionDatabaseStoreError("MongoDB connection was not initialized.")
        _client.admin.command("ping")
        return {
            "configured": True,
            "connected": True,
            "bucket": _BUCKET_NAME,
            "message": "Session database persistence is connected.",
        }
    except Exception as exc:
        return {
            "configured": True,
            "connected": False,
            "bucket": _BUCKET_NAME,
            "message": f"Session database persistence failed: {type(exc).__name__}",
        }
