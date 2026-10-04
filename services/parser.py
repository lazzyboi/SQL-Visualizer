import re
from typing import Any

import sqlparse


def _clean_identifier(value: str) -> str:
    value = value.strip().strip(";,()")
    return value.replace("`", "").replace('"', "").replace("[", "").replace("]", "")


def _extract_where(query: str) -> str:
    match = re.search(
        r"\bWHERE\b\s+(.*?)(?=\s+GROUP\s+BY\b|\s+ORDER\s+BY\b|\s+HAVING\b|\s+LIMIT\b|\s+OFFSET\b|$)",
        query,
        re.IGNORECASE | re.DOTALL,
    )
    return " ".join(match.group(1).split()) if match else ""


def _extract_from(query: str) -> str:
    match = re.search(r"\bFROM\b\s+([`\"\[\]\w.]+)", query, re.IGNORECASE)
    return _clean_identifier(match.group(1)) if match else ""


def _extract_select(query: str) -> list[str]:
    match = re.search(r"\bSELECT\b\s+(.*?)(?=\s+FROM\b)", query, re.IGNORECASE | re.DOTALL)
    if not match:
        return []
    return [part.strip() for part in match.group(1).split(",") if part.strip()]


def _extract_joins(query: str) -> list[dict[str, Any]]:
    pattern = re.compile(
        r"\b(?:(LEFT|RIGHT|FULL|INNER|OUTER|CROSS)\s+)?JOIN\s+"
        r"([`\"\[\]\w.]+)(?:\s+(?:AS\s+)?([`\"\w]+))?\s+"
        r"ON\s+(.*?)(?=\s+(?:(?:LEFT|RIGHT|FULL|INNER|OUTER|CROSS)\s+)?JOIN\b"
        r"|\s+WHERE\b|\s+GROUP\s+BY\b|\s+ORDER\s+BY\b|\s+HAVING\b|\s+LIMIT\b|$)",
        re.IGNORECASE | re.DOTALL,
    )
    joins = []
    for match in pattern.finditer(query):
        joins.append(
            {
                "type": (match.group(1) or "INNER").upper(),
                "table": _clean_identifier(match.group(2)),
                "alias": _clean_identifier(match.group(3) or ""),
                "on": " ".join(match.group(4).split()),
            }
        )
    return joins


def _extract_alias_map(query: str, joins: list[dict[str, Any]]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    from_match = re.search(
        r"\bFROM\b\s+([`\"\[\]\w.]+)(?:\s+(?:AS\s+)?([`\"\w]+))?",
        query,
        re.IGNORECASE,
    )
    if from_match:
        table = _clean_identifier(from_match.group(1))
        alias = _clean_identifier(from_match.group(2) or "")
        aliases[table.lower()] = table
        if alias:
            aliases[alias.lower()] = table
    for join in joins:
        table = join["table"]
        aliases[table.lower()] = table
        if join.get("alias"):
            aliases[join["alias"].lower()] = table
    return aliases


def _build_er_schema(parsed: dict[str, Any], query: str) -> dict[str, Any]:
    tables: dict[str, dict[str, Any]] = {}
    relationships = []
    aliases = _extract_alias_map(query, parsed["joins"])

    def add_table(name: str) -> None:
        if not name:
            return
        table_id = name.lower()
        tables.setdefault(table_id, {"id": table_id, "name": name, "columns": []})

    def add_column(table_ref: str, column: str) -> None:
        table = aliases.get(table_ref.lower(), table_ref)
        if not table:
            return
        add_table(table)
        obj = tables[table.lower()]
        if column and column not in obj["columns"]:
            obj["columns"].append(column)

    add_table(parsed["from"])
    for join in parsed["joins"]:
        add_table(join["table"])

    for join in parsed["joins"]:
        for left, right in re.findall(r"([\w`\"\[\].]+)\s*=\s*([\w`\"\[\].]+)", join["on"]):
            left = _clean_identifier(left)
            right = _clean_identifier(right)
            left_table, _, left_col = left.partition(".")
            right_table, _, right_col = right.partition(".")
            if not left_col or not right_col:
                continue
            add_column(left_table, left_col)
            add_column(right_table, right_col)
            lt = aliases.get(left_table.lower(), left_table).lower()
            rt = aliases.get(right_table.lower(), right_table).lower()
            relationships.append({"source": lt, "target": rt, "label": f"{left_col} = {right_col}", "type": "JOIN"})

    for expression in parsed["select"] + ([parsed["where"]] if parsed["where"] else []):
        for table_ref, column in re.findall(r"([A-Za-z_][\w]*)\.([A-Za-z_][\w]*)", expression):
            add_column(table_ref, column)

    for table in tables.values():
        if not table["columns"]:
            table["columns"] = ["(columns inferred from query)"]

    return {"tables": list(tables.values()), "relationships": relationships}


def validate_query(query: str) -> list[str]:
    errors: list[str] = []
    if not query or not query.strip():
        return ["Enter a SQL query before analyzing it."]

    statements = [stmt.strip() for stmt in sqlparse.split(query) if stmt.strip()]
    if len(statements) > 1:
        errors.append("Multiple SQL statements were detected. Analyze one query at a time.")
        return errors

    normalized = " ".join(query.split())
    if not re.match(r"^(SELECT|WITH)\b", normalized, re.IGNORECASE):
        errors.append("This version of SQL Visualizer is focused on SELECT-style queries. Start with SELECT or WITH.")

    if re.search(r"\bSELECT\b", query, re.IGNORECASE) and not re.search(r"\bFROM\b", query, re.IGNORECASE):
        errors.append("A SELECT query normally needs a FROM clause unless it is selecting constant expressions only.")

    if query.count("(") != query.count(")"):
        errors.append("Parentheses are unbalanced. Check that every opening parenthesis has a matching closing parenthesis.")

    if re.search(r"\bWHERE\s*(GROUP|ORDER|LIMIT|$)", query, re.IGNORECASE):
        errors.append("The WHERE clause appears to be missing a condition.")

    return errors


def parse_query(query: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "select": [],
        "from": "",
        "joins": [],
        "where": "",
        "tables": [],
        "validation_errors": [],
        "er": {"tables": [], "relationships": []},
    }
    query = query or ""
    result["validation_errors"] = validate_query(query)
    if not query.strip() or result["validation_errors"]:
        return result

    result["select"] = _extract_select(query)
    result["from"] = _extract_from(query)
    result["joins"] = _extract_joins(query)
    result["where"] = _extract_where(query)
    result["tables"] = list(dict.fromkeys([table for table in [result["from"], *[j["table"] for j in result["joins"]]] if table]))
    result["er"] = _build_er_schema(result, query)
    return result
