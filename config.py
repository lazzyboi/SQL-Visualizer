import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

APP_ENV = os.getenv("APP_ENV", "development").strip().lower()
PORT = int(os.getenv("PORT", "10000"))

DATA_DIR = os.getenv("DATA_DIR", str(BASE_DIR / "data"))
USERS_DIR = os.getenv("USERS_DIR", str(Path(DATA_DIR) / "users"))
AUTH_DB_PATH = os.getenv("AUTH_DB_PATH", str(Path(DATA_DIR) / "app.db"))
HISTORY_FILE = os.getenv("HISTORY_FILE", str(Path(DATA_DIR) / "history.json"))

MONGO_URI = os.getenv("MONGO_URI", "").strip()
MONGO_DB = os.getenv("MONGO_DB", "sqlviz").strip()
MONGO_USERS_COLLECTION = os.getenv("MONGO_USERS_COLLECTION", "users").strip()
MONGO_HISTORY_COLLECTION = os.getenv("MONGO_HISTORY_COLLECTION", "query_history").strip()
MONGO_SERVER_SELECTION_TIMEOUT_MS = int(os.getenv("MONGO_SERVER_SELECTION_TIMEOUT_MS", "3000"))

ADMIN_EMAILS = {
    item.strip().lower()
    for item in os.getenv("ADMIN_EMAILS", "").split(",")
    if item.strip()
}

SECRET_KEY = os.getenv("SECRET_KEY", "").strip()
if not SECRET_KEY:
    if APP_ENV == "production":
        raise RuntimeError("SECRET_KEY must be configured in production.")
    SECRET_KEY = "development-only-change-me"

SESSION_COOKIE_SECURE = os.getenv(
    "SESSION_COOKIE_SECURE",
    "1" if APP_ENV == "production" else "0",
) == "1"
SESSION_LIFETIME_SECONDS = int(os.getenv("SESSION_LIFETIME_SECONDS", str(8 * 60 * 60)))

AI_PROVIDER = os.getenv("AI_PROVIDER", "fallback").lower()
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/generate")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:4b")
AI_TIMEOUT = float(os.getenv("AI_TIMEOUT", "20"))

MAX_HISTORY = int(os.getenv("MAX_HISTORY", "200"))
MAX_OUTPUT_ROWS = int(os.getenv("MAX_OUTPUT_ROWS", "500"))
ADMIN_QUERY_LIMIT = int(os.getenv("ADMIN_QUERY_LIMIT", "100"))

# In production, MongoDB Atlas is the source of truth for accounts/history.
# The local filesystem is used for per-user SQLite sandbox databases.
if APP_ENV == "production" and not MONGO_URI:
    raise RuntimeError("MONGO_URI must be configured in production.")


def mongo_connection_metadata() -> dict:
    if not MONGO_URI:
        return {"configured": False, "host": None, "username": None}
    parsed = urlparse(MONGO_URI)
    return {
        "configured": True,
        "host": parsed.hostname,
        "username": parsed.username,
    }
