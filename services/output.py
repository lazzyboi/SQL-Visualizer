import os
import re
import sqlite3
from typing import Any

from config import MAX_OUTPUT_ROWS


def _is_read_only_statement(query: str) -> bool:
    return bool(re.match(r"^\s*(SELECT|WITH)\b", query or "", re.IGNORECASE))


def execute_read_only(query: str, db_path: str) -> dict[str, Any]:
    if not db_path:
        return {
            "available": False,
            "message": "No personal database is configured for this user.",
            "columns": [],
            "rows": [],
        }

    if not _is_read_only_statement(query):
        return {
            "available": False,
            "message": "Only SELECT/WITH queries can be executed in the read-only sandbox.",
            "columns": [],
            "rows": [],
        }

    if ";" in query.rstrip().rstrip(";"):
        return {
            "available": False,
            "message": "Run one SQL statement at a time in the sandbox.",
            "columns": [],
            "rows": [],
        }

    path = os.path.abspath(db_path)
    if not os.path.exists(path):
        return {
            "available": False,
            "message": "Your personal sandbox database has not been created yet.",
            "columns": [],
            "rows": [],
        }

    connection = None
    try:
        uri = f"file:{path}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        cursor = connection.execute(query.rstrip().rstrip(";"))
        rows = [dict(row) for row in cursor.fetchmany(MAX_OUTPUT_ROWS)]
        columns = [description[0] for description in cursor.description or []]
        return {
            "available": True,
            "message": f"Showing up to {MAX_OUTPUT_ROWS} rows from your personal read-only sandbox.",
            "columns": columns,
            "rows": rows,
        }
    except Exception as exc:
        return {
            "available": True,
            "message": f"The query could not be executed: {exc}",
            "columns": [],
            "rows": [],
        }
    finally:
        if connection is not None:
            connection.close()
