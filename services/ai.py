import json
from typing import Any

import requests

from config import AI_PROVIDER, AI_TIMEOUT, OLLAMA_MODEL, OLLAMA_URL


def _fallback(parsed: dict[str, Any], security: list[dict[str, Any]], performance: dict[str, Any]) -> dict[str, Any]:
    summary: list[str] = []
    teaching: list[str] = []
    suggestions: list[str] = []

    summary.extend(parsed.get("validation_errors", []))
    if parsed.get("select"):
        summary.append(f"The query selects {len(parsed['select'])} expression(s).")
    if parsed.get("from"):
        summary.append(f"The base table is `{parsed['from']}`.")
    if parsed.get("joins"):
        summary.append(f"The query contains {len(parsed['joins'])} JOIN operation(s).")
        teaching.append("A JOIN combines rows from related tables using a condition such as `users.id = orders.user_id`.")
    if parsed.get("where"):
        teaching.append("WHERE filters rows before the result is returned. A selective condition can reduce work on the database.")
    else:
        suggestions.append("Consider whether a WHERE condition is appropriate for the result you want.")
    if "*" in parsed.get("select", []):
        summary.append("`SELECT *` requests every available column from the selected source tables.")
        suggestions.append("Select only the columns the application actually needs.")
    if security:
        summary.append("The security scanner found one or more patterns that should be reviewed before executing the SQL with untrusted input.")
        suggestions.append("Use parameterized queries when application code inserts user-provided values into SQL.")
    if performance.get("available"):
        for insight in performance.get("insights", [])[:3]:
            summary.append(insight)
    elif performance.get("message"):
        summary.append(performance["message"])
    teaching.append("The Performance tab shows the database's actual SQLite execution plan rather than a made-up cost score.")

    return {"provider": "fallback", "summary": summary, "teaching": teaching, "suggestions": suggestions, "raw": None}


def _prompt(parsed: dict[str, Any], security: list[dict[str, Any]], performance: dict[str, Any]) -> str:
    return f"""You are a SQL tutor inside a web application.\nExplain the user's SQL accurately and simply. Never invent schema details.\nReturn JSON with keys summary, teaching, suggestions, each an array of concise strings.\nFocus on errors, why they happen, how to fix them, database behavior, and what the user can learn.\n\nParsed query:\n{json.dumps(parsed, indent=2)}\n\nSecurity scan:\n{json.dumps(security, indent=2)}\n\nPerformance plan:\n{json.dumps(performance, indent=2)}\n"""


def _ollama(parsed: dict[str, Any], security: list[dict[str, Any]], performance: dict[str, Any]) -> dict[str, Any] | None:
    try:
        response = requests.post(
            OLLAMA_URL,
            json={
                "model": OLLAMA_MODEL,
                "prompt": _prompt(parsed, security, performance),
                "stream": False,
                "format": "json",
            },
            timeout=AI_TIMEOUT,
        )
        response.raise_for_status()
        raw = response.json().get("response", "")
        data = json.loads(raw)
        return {
            "provider": f"ollama:{OLLAMA_MODEL}",
            "summary": data.get("summary", []),
            "teaching": data.get("teaching", []),
            "suggestions": data.get("suggestions", []),
            "raw": raw,
        }
    except Exception:
        return None


def analyze(parsed: dict[str, Any], security: list[dict[str, Any]] | None = None, performance: dict[str, Any] | None = None):
    security = security or []
    performance = performance or {"available": False, "plan": [], "insights": []}
    if AI_PROVIDER == "ollama":
        generated = _ollama(parsed, security, performance)
        if generated:
            return generated
    return _fallback(parsed, security, performance)
