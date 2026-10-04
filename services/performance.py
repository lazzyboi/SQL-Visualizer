import os
import re
import sqlite3
from typing import Any


def _safe_select(query: str) -> bool:
    return bool(re.match(r"^\s*(SELECT|WITH)\b", query or "", re.IGNORECASE))


def explain_query(query: str, db_path: str) -> dict[str, Any]:
    if not _safe_select(query):
        return {"available": False, "message": "Performance analysis is available for SELECT/WITH queries.", "plan": [], "insights": []}
    clean = query.strip().rstrip(";")
    if ";" in clean:
        return {"available": False, "message": "Run one SQL statement at a time for performance analysis.", "plan": [], "insights": []}
    if not os.path.exists(db_path):
        return {"available": False, "message": "Your personal sandbox database is not available yet.", "plan": [], "insights": []}

    connection = None
    try:
        connection = sqlite3.connect(f"file:{os.path.abspath(db_path)}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        rows = connection.execute(f"EXPLAIN QUERY PLAN {clean}").fetchall()
        plan = [dict(row) for row in rows]
        details = [str(row["detail"]) for row in rows]
        insights = []
        for detail in details:
            upper = detail.upper()
            if "SCAN" in upper and "USING INDEX" not in upper and "USING COVERING INDEX" not in upper:
                insights.append(f"A table scan appears in the plan: {detail}")
            if "SEARCH" in upper and "USING INDEX" in upper:
                insights.append(f"SQLite reports indexed lookup: {detail}")
            if "USE TEMP B-TREE" in upper:
                insights.append("SQLite is using a temporary B-tree, commonly associated with sorting or grouping work.")
        if not insights:
            insights.append("The database returned an execution plan. The AI Tutor can explain the individual steps.")
        return {"available": True, "message": "SQLite execution plan generated for your personal sandbox.", "plan": plan, "insights": insights}
    except Exception as exc:
        return {"available": True, "message": f"The execution plan could not be generated: {exc}", "plan": [], "insights": []}
    finally:
        if connection is not None:
            connection.close()
