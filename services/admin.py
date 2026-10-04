import sqlite3
from typing import Any

from services.auth import database_schema, get_user_db_path


def sandbox_data(user_id: str, table: str, limit: int = 25) -> dict[str, Any]:
    path = get_user_db_path(user_id)
    database_schema(user_id)
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        valid_tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        if table not in valid_tables:
            raise ValueError("Unknown sandbox table")
        safe_table = table.replace('"', '""')
        columns = [row[1] for row in conn.execute(f'PRAGMA table_info("{safe_table}")').fetchall()]
        rows = [dict(row) for row in conn.execute(f'SELECT * FROM "{safe_table}" LIMIT ?', (max(1, min(limit, 100)),)).fetchall()]
        return {"table": table, "columns": columns, "rows": rows, "database_path": path}
