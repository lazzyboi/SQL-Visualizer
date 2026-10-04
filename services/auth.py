import os
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Any

from pymongo import ASCENDING, MongoClient
from pymongo.server_api import ServerApi
from pymongo.errors import DuplicateKeyError, PyMongoError
from werkzeug.security import check_password_hash, generate_password_hash

from config import (
    ADMIN_EMAILS,
    APP_ENV,
    mongo_connection_metadata,
    AUTH_DB_PATH,
    MONGO_DB,
    MONGO_SERVER_SELECTION_TIMEOUT_MS,
    MONGO_URI,
    MONGO_USERS_COLLECTION,
    USERS_DIR,
)

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_mongo_client: MongoClient | None = None
_mongo_users = None


def _connect() -> sqlite3.Connection:
    directory = os.path.dirname(AUTH_DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(AUTH_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _mongo_collection():
    global _mongo_client, _mongo_users
    if not MONGO_URI:
        return None
    if _mongo_users is None:
        _mongo_client = MongoClient(
            MONGO_URI,
            serverSelectionTimeoutMS=MONGO_SERVER_SELECTION_TIMEOUT_MS,
            connectTimeoutMS=MONGO_SERVER_SELECTION_TIMEOUT_MS,
            tz_aware=True,
            server_api=ServerApi("1"),
        )
        _mongo_users = _mongo_client[MONGO_DB][MONGO_USERS_COLLECTION]
    return _mongo_users


def mongo_status() -> dict[str, Any]:
    if not MONGO_URI:
        return {"configured": False, "connected": False, "host": None, "username": None, "message": "MongoDB is not configured; local development fallback is active."}
    try:
        collection = _mongo_collection()
        _mongo_client.admin.command("ping")
        return {
            "configured": True,
            "connected": True,
            "database": MONGO_DB,
            "collection": MONGO_USERS_COLLECTION,
            **mongo_connection_metadata(),
            "message": "MongoDB connection is healthy.",
        }
    except Exception as exc:
        return {
            "configured": True,
            "connected": False,
            "database": MONGO_DB,
            "collection": MONGO_USERS_COLLECTION,
            **mongo_connection_metadata(),
            "message": f"MongoDB connection failed: {type(exc).__name__}",
        }


def init_auth_db() -> None:
    os.makedirs(USERS_DIR, exist_ok=True)
    collection = _mongo_collection()
    if collection is not None:
        try:
            collection.create_index([("email", ASCENDING)], unique=True, name="uniq_email")
            collection.create_index([("id", ASCENDING)], unique=True, name="uniq_user_id")
            if ADMIN_EMAILS:
                collection.update_many({"email": {"$in": list(ADMIN_EMAILS)}}, {"$set": {"role": "admin"}})
            if APP_ENV == "production":
                _mongo_client.admin.command("ping")
            return
        except PyMongoError as exc:
            if APP_ENV == "production":
                raise RuntimeError("MongoDB is configured but not reachable.") from exc

    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                created_at TEXT NOT NULL
            )
            """
        )
        # Upgrade an existing local database created by Phase 2.
        columns = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "role" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)")
        if ADMIN_EMAILS:
            placeholders = ",".join("?" for _ in ADMIN_EMAILS)
            conn.execute(f"UPDATE users SET role = 'admin' WHERE lower(email) IN ({placeholders})", tuple(ADMIN_EMAILS))
        conn.commit()


def _row_to_user(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": row["id"],
        "email": row["email"],
        "role": row["role"] if "role" in row.keys() else "user",
        "created_at": row["created_at"],
    }


def _effective_role(email: str, stored_role: str | None) -> str:
    return "admin" if email.strip().lower() in ADMIN_EMAILS else (stored_role or "user")


def _doc_to_user(doc: dict[str, Any] | None) -> dict[str, Any] | None:
    if not doc:
        return None
    return {
        "id": doc.get("id"),
        "email": doc.get("email"),
        "role": _effective_role(doc.get("email", ""), doc.get("role")),
        "created_at": doc.get("created_at"),
    }


def is_admin(user: dict[str, Any] | None) -> bool:
    return bool(user and user.get("role") == "admin")


def get_user(user_id: str) -> dict[str, Any] | None:
    collection = _mongo_collection()
    if collection is not None:
        return _doc_to_user(collection.find_one({"id": user_id}))
    with _connect() as conn:
        row = conn.execute("SELECT id, email, role, created_at FROM users WHERE id = ?", (user_id,)).fetchone()
    return _row_to_user(row)


def register_user(email: str, password: str) -> dict[str, Any]:
    email = (email or "").strip().lower()
    password = password or ""
    if not _EMAIL_RE.match(email):
        raise ValueError("Enter a valid email address.")
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters long.")

    user_id = secrets.token_urlsafe(16)
    created_at = datetime.now(timezone.utc).isoformat()
    role = "admin" if email in ADMIN_EMAILS else "user"
    password_hash = generate_password_hash(password)
    document = {
        "id": user_id,
        "email": email,
        "password_hash": password_hash,
        "role": role,
        "created_at": created_at,
    }

    collection = _mongo_collection()
    if collection is not None:
        try:
            collection.insert_one(document)
        except DuplicateKeyError as exc:
            raise ValueError("An account with that email already exists.") from exc
        seed_user_database(user_id)
        return {"id": user_id, "email": email, "role": role, "created_at": created_at}

    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO users (id, email, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, email, password_hash, role, created_at),
            )
            conn.commit()
    except sqlite3.IntegrityError as exc:
        raise ValueError("An account with that email already exists.") from exc

    seed_user_database(user_id)
    return {"id": user_id, "email": email, "role": role, "created_at": created_at}


def authenticate_user(email: str, password: str) -> dict[str, Any] | None:
    email = (email or "").strip().lower()
    collection = _mongo_collection()
    if collection is not None:
        row = collection.find_one({"email": email})
        if row is None or not check_password_hash(row.get("password_hash", ""), password or ""):
            return None
        return _doc_to_user(row)

    with _connect() as conn:
        row = conn.execute(
            "SELECT id, email, password_hash, role, created_at FROM users WHERE email = ?",
            (email,),
        ).fetchone()
    if row is None or not check_password_hash(row["password_hash"], password or ""):
        return None
    return _row_to_user(row)


def get_user_db_path(user_id: str) -> str:
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "", user_id)
    if not safe_id:
        raise ValueError("Invalid user id")
    user_dir = os.path.join(USERS_DIR, safe_id)
    os.makedirs(user_dir, exist_ok=True)
    return os.path.join(user_dir, "sandbox.db")


def seed_user_database(user_id: str) -> None:
    path = get_user_db_path(user_id)
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            PRAGMA foreign_keys = ON;

            CREATE TABLE IF NOT EXISTS departments (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE
            );

            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                department_id INTEGER,
                FOREIGN KEY (department_id) REFERENCES departments(id)
            );

            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                price REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                status TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS order_items (
                id INTEGER PRIMARY KEY,
                order_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL,
                FOREIGN KEY (order_id) REFERENCES orders(id),
                FOREIGN KEY (product_id) REFERENCES products(id)
            );

            INSERT OR IGNORE INTO departments (id, name) VALUES
                (1, 'Computer Science'),
                (2, 'Engineering'),
                (3, 'Management');

            INSERT OR IGNORE INTO users (id, name, email, department_id) VALUES
                (1, 'Alice', 'alice@example.com', 1),
                (2, 'Rahul', 'rahul@example.com', 2),
                (3, 'Priya', 'priya@example.com', 1),
                (4, 'David', 'david@example.com', 3);

            INSERT OR IGNORE INTO products (id, name, price) VALUES
                (1, 'Keyboard', 45.00),
                (2, 'Mouse', 25.00),
                (3, 'Monitor', 210.00),
                (4, 'Headset', 80.00);

            INSERT OR IGNORE INTO orders (id, user_id, amount, status) VALUES
                (101, 1, 70.00, 'PAID'),
                (102, 1, 210.00, 'SHIPPED'),
                (103, 2, 105.00, 'PAID'),
                (104, 3, 290.00, 'PROCESSING'),
                (105, 4, 80.00, 'PAID');

            INSERT OR IGNORE INTO order_items (id, order_id, product_id, quantity) VALUES
                (1, 101, 1, 1),
                (2, 101, 2, 1),
                (3, 102, 3, 1),
                (4, 103, 1, 1),
                (5, 103, 2, 1),
                (6, 104, 3, 1),
                (7, 104, 4, 1),
                (8, 105, 4, 1);
            """
        )
        conn.commit()


def database_schema(user_id: str) -> list[dict[str, Any]]:
    path = get_user_db_path(user_id)
    seed_user_database(user_id)
    with sqlite3.connect(path) as conn:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        result = []
        for table in tables:
            safe_table = table.replace('"', '""')
            columns = conn.execute(f'PRAGMA table_info("{safe_table}")').fetchall()
            count = conn.execute(f'SELECT COUNT(*) FROM "{safe_table}"').fetchone()[0]
            result.append(
                {
                    "name": table,
                    "rows": count,
                    "columns": [
                        {
                            "name": col[1],
                            "type": col[2],
                            "primary_key": bool(col[5]),
                            "nullable": not bool(col[3]),
                        }
                        for col in columns
                    ],
                }
            )
        return result


def admin_list_users(limit: int = 100) -> list[dict[str, Any]]:
    collection = _mongo_collection()
    if collection is not None:
        docs = list(collection.find({}, {"_id": 0}).sort("created_at", -1).limit(limit))
        return docs
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, email, password_hash, role, created_at FROM users ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]

