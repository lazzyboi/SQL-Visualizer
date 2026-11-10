"""Canonical, schema-first ER model builder for SQL Visualizer.

The database schema is the source of truth. SQL is used only to determine
query focus/highlighting. The resulting model is shared by the ER renderer,
AI Tutor, and history persistence layer.
"""

from __future__ import annotations

import os
import re
import sqlite3
from typing import Any


ER_MODEL_VERSION = 2
SCHEMA_VERSION = 2


_IDENTIFIER_RE = re.compile(r"([A-Za-z_][\w]*)\.([A-Za-z_][\w]*)")
_JOIN_SIDE_RE = re.compile(r"([A-Za-z_][\w]*)\.([A-Za-z_][\w]*)")


def _safe_identifier(value: str) -> str:
    return (value or "").strip().strip("`\"[]")


def _qualified_columns(parsed: dict[str, Any]) -> dict[str, set[str]]:
    """Collect table-qualified columns referenced by the SQL query."""
    result: dict[str, set[str]] = {}

    def add(expression: str | None) -> None:
        if not expression:
            return
        for table, column in _IDENTIFIER_RE.findall(expression):
            result.setdefault(table.lower(), set()).add(column.lower())

    for expression in parsed.get("select") or []:
        add(str(expression))
    add(parsed.get("where"))

    for join in parsed.get("joins") or []:
        add(str(join.get("on") or ""))

    return result


def _query_tables(parsed: dict[str, Any]) -> set[str]:
    return {
        str(name).lower()
        for name in (parsed.get("tables") or [])
        if str(name).strip()
    }


def _unique_key_sets(conn: sqlite3.Connection, table: str) -> list[list[str]]:
    safe = table.replace('"', '""')
    keys: list[list[str]] = []

    for row in conn.execute(f'PRAGMA index_list("{safe}")').fetchall():
        # seq, name, unique, origin, partial (origin/partial are available in
        # modern SQLite; only the first three are required here).
        index_name = row[1]
        is_unique = bool(row[2])
        if not is_unique:
            continue
        columns = [
            info[2]
            for info in conn.execute(
                f'PRAGMA index_info("{index_name.replace(chr(34), chr(34) * 2)}")'
            ).fetchall()
            if info[2]
        ]
        if columns:
            keys.append(columns)

    # Deterministic de-duplication while preserving database order.
    seen: set[tuple[str, ...]] = set()
    unique: list[list[str]] = []
    for key in keys:
        marker = tuple(key)
        if marker not in seen:
            seen.add(marker)
            unique.append(key)
    return unique


def _foreign_keys(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    safe = table.replace('"', '""')
    rows = conn.execute(f'PRAGMA foreign_key_list("{safe}")').fetchall()
    grouped: dict[int, list[sqlite3.Row | tuple[Any, ...]]] = {}
    for row in rows:
        grouped.setdefault(int(row[0]), []).append(row)

    relationships: list[dict[str, Any]] = []
    for fk_id, group in grouped.items():
        group.sort(key=lambda item: int(item[1]))
        parent_table = _safe_identifier(str(group[0][2]))
        child_columns = [_safe_identifier(str(item[3])) for item in group]
        parent_columns = [_safe_identifier(str(item[4])) for item in group]
        relationships.append(
            {
                "constraint_id": fk_id,
                "child_table": table,
                "parent_table": parent_table,
                "child_columns": child_columns,
                "parent_columns": parent_columns,
                "on_update": str(group[0][5] or "NO ACTION"),
                "on_delete": str(group[0][6] or "NO ACTION"),
            }
        )
    return relationships


def _column_nullable_map(entity: dict[str, Any]) -> dict[str, bool]:
    return {
        str(attr["name"]).lower(): bool(attr.get("nullable", True))
        for attr in entity.get("attributes", [])
    }


def _is_unique_key(columns: list[str], unique_keys: list[list[str]]) -> bool:
    target = tuple(column.lower() for column in columns)
    return any(tuple(col.lower() for col in key) == target for key in unique_keys)


def introspect_sqlite(db_path: str) -> dict[str, Any]:
    """Read the complete SQLite schema including PK, UQ and FK metadata."""
    if not db_path or not os.path.exists(db_path):
        raise FileNotFoundError("SQLite sandbox database is not available.")

    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        table_rows = connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()

        entities: list[dict[str, Any]] = []
        table_map: dict[str, dict[str, Any]] = {}

        for table_row in table_rows:
            table = str(table_row["name"])
            safe = table.replace('"', '""')
            column_rows = connection.execute(f'PRAGMA table_info("{safe}")').fetchall()
            unique_keys = _unique_key_sets(connection, table)

            primary_key = [
                str(row[1])
                for row in sorted(
                    [row for row in column_rows if int(row[5] or 0) > 0],
                    key=lambda row: int(row[5]),
                )
            ]

            attributes: list[dict[str, Any]] = []
            for row in column_rows:
                name = str(row[1])
                pk_position = int(row[5] or 0)
                is_pk = pk_position > 0
                is_unique = any(
                    len(key) == 1 and key[0].lower() == name.lower()
                    for key in unique_keys
                )
                attributes.append(
                    {
                        "id": f"{table}.{name}".lower(),
                        "name": name,
                        "type": str(row[2] or "TEXT"),
                        "nullable": not bool(row[3]),
                        "default": row[4],
                        "key": "PK" if is_pk else ("UQ" if is_unique else None),
                        "pk_position": pk_position or None,
                        "foreign_key": False,
                        "references": None,
                        "query_relevant": False,
                    }
                )

            entity = {
                "id": table.lower(),
                "name": table,
                "attributes": attributes,
                "primary_key": primary_key,
                "unique_keys": unique_keys,
                "weak": False,
                "query_relevant": False,
                "row_count": int(connection.execute(f'SELECT COUNT(*) FROM "{safe}"').fetchone()[0]),
            }
            entities.append(entity)
            table_map[table.lower()] = entity

        relationships: list[dict[str, Any]] = []
        for entity in entities:
            child_table = entity["name"]
            child_attr_map = {attr["name"].lower(): attr for attr in entity["attributes"]}
            for fk in _foreign_keys(connection, child_table):
                parent = table_map.get(fk["parent_table"].lower())
                if not parent:
                    continue

                nullable_map = _column_nullable_map(entity)
                child_columns = fk["child_columns"]
                parent_columns = fk["parent_columns"]
                unique_child_fk = _is_unique_key(child_columns, entity["unique_keys"])
                identifying = all(
                    column.lower() in {pk.lower() for pk in entity["primary_key"]}
                    for column in child_columns
                )
                entity["weak"] = entity["weak"] or identifying

                for child_col, parent_col in zip(child_columns, parent_columns):
                    attr = child_attr_map.get(child_col.lower())
                    if attr:
                        attr["foreign_key"] = True
                        attr["key"] = "PK/FK" if attr["key"] == "PK" else "FK"
                        attr["references"] = {
                            "table": parent["name"],
                            "column": parent_col,
                        }

                relationship_id = (
                    f"fk:{child_table.lower()}:{'|'.join(c.lower() for c in child_columns)}"
                )
                relationship = {
                    "id": relationship_id,
                    "name": f"{child_table} → {parent['name']}",
                    "from": parent["id"],
                    "to": entity["id"],
                    "from_attributes": [f"{parent['name']}.{column}" for column in parent_columns],
                    "to_attributes": [f"{child_table}.{column}" for column in child_columns],
                    "cardinality": "1:1" if unique_child_fk else "1:N",
                    "participation": {
                        "from": "partial",
                        "to": "partial" if any(nullable_map.get(c.lower(), True) for c in child_columns) else "total",
                    },
                    "identifying": identifying,
                    "weak_entity": entity["id"] if identifying else None,
                    "query_relevant": False,
                    "constraint": {
                        "on_update": fk["on_update"],
                        "on_delete": fk["on_delete"],
                    },
                }
                relationships.append(relationship)

        return {
            "schema_version": SCHEMA_VERSION,
            "entities": entities,
            "relationships": relationships,
        }
    finally:
        connection.close()


def build_er_model(db_path: str, parsed: dict[str, Any], query: str = "") -> dict[str, Any]:
    """Build the canonical ER model from real schema plus query highlighting."""
    schema = introspect_sqlite(db_path)
    focused_tables = _query_tables(parsed)
    qualified_columns = _qualified_columns(parsed)

    # Build alias map from FROM/JOIN parsing so users can write u.id instead of
    # users.id while the canonical model still points to the real entity.
    aliases: dict[str, str] = {}
    if parsed.get("from"):
        aliases[str(parsed["from"]).lower()] = str(parsed["from"])
    for join in parsed.get("joins") or []:
        table = str(join.get("table") or "")
        alias = str(join.get("alias") or "")
        if table:
            aliases[table.lower()] = table
        if alias:
            aliases[alias.lower()] = table

    focused_tables = {
        aliases.get(table, table)
        for table in focused_tables
    }
    focused_tables = {table.lower() for table in focused_tables}

    for entity in schema["entities"]:
        entity["query_relevant"] = entity["id"] in focused_tables
        for attr in entity["attributes"]:
            direct = qualified_columns.get(entity["name"].lower(), set())
            alias_columns = {
                column
                for alias, table in aliases.items()
                if table.lower() == entity["name"].lower()
                for column in qualified_columns.get(alias, set())
            }
            attr["query_relevant"] = attr["name"].lower() in (direct | alias_columns)

    for relationship in schema["relationships"]:
        relationship["query_relevant"] = (
            relationship["from"] in focused_tables and relationship["to"] in focused_tables
        )

    # Query JOIN relationships that are not declared as FKs are represented as
    # highlighted, inferred relationships. The schema remains authoritative for
    # actual constraints.
    for join in parsed.get("joins") or []:
        conditions = str(join.get("on") or "")
        for left, right in _JOIN_SIDE_RE.findall(conditions):
            pass
        pairs = re.findall(
            r"([A-Za-z_][\w]*)\.([A-Za-z_][\w]*)\s*=\s*([A-Za-z_][\w]*)\.([A-Za-z_][\w]*)",
            conditions,
        )
        for left_table, left_col, right_table, right_col in pairs:
            left_real = aliases.get(left_table.lower(), left_table)
            right_real = aliases.get(right_table.lower(), right_table)
            if left_real.lower() not in focused_tables or right_real.lower() not in focused_tables:
                continue
            left_id = left_real.lower()
            right_id = right_real.lower()
            existing = next(
                (
                    relation
                    for relation in schema["relationships"]
                    if {relation["from"], relation["to"]} == {left_id, right_id}
                    and any(left_col.lower() in item.lower() for item in relation["from_attributes"] + relation["to_attributes"])
                ),
                None,
            )
            if existing:
                existing["query_relevant"] = True
                continue
            schema["relationships"].append(
                {
                    "id": f"query:{left_id}.{left_col}={right_id}.{right_col}",
                    "name": f"JOIN: {left_real}.{left_col} = {right_real}.{right_col}",
                    "from": left_id,
                    "to": right_id,
                    "from_attributes": [f"{left_real}.{left_col}"],
                    "to_attributes": [f"{right_real}.{right_col}"],
                    "cardinality": "JOIN",
                    "participation": {"from": "unknown", "to": "unknown"},
                    "identifying": False,
                    "weak_entity": None,
                    "query_relevant": True,
                    "inferred_from_query": True,
                    "constraint": None,
                }
            )

    query_summary = {
        "tables": sorted(focused_tables),
        "attributes": sorted(
            [
                f"{entity['name']}.{attr['name']}"
                for entity in schema["entities"]
                for attr in entity["attributes"]
                if attr.get("query_relevant")
            ]
        ),
        "relationships": [
            relationship["id"]
            for relationship in schema["relationships"]
            if relationship.get("query_relevant")
        ],
        "sql": query or "",
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "er_model_version": ER_MODEL_VERSION,
        "notation": {
            "style": "chen",
            "entity": "rectangle",
            "attribute": "oval",
            "relationship": "diamond",
            "primary_key": "underline",
            "weak_entity": "double_rectangle",
            "identifying_relationship": "double_diamond",
            "multivalued_attribute": "double_oval",
            "derived_attribute": "dashed_oval",
        },
        "entities": schema["entities"],
        "relationships": schema["relationships"],
        "query_focus": query_summary,
    }


def validate_er_model(model: Any) -> tuple[bool, list[str]]:
    """Validate a persisted ER model before it is rendered/restored."""
    errors: list[str] = []
    if not isinstance(model, dict):
        return False, ["ER model is not an object."]
    if model.get("schema_version") != SCHEMA_VERSION:
        errors.append("Unsupported schema version.")
    if model.get("er_model_version") != ER_MODEL_VERSION:
        errors.append("Unsupported ER model version.")

    entities = model.get("entities")
    relationships = model.get("relationships")
    if not isinstance(entities, list) or not isinstance(relationships, list):
        errors.append("ER model is missing entities or relationships.")
        return False, errors

    entity_ids = {str(entity.get("id")) for entity in entities if isinstance(entity, dict)}
    if len(entity_ids) != len(entities):
        errors.append("ER model contains duplicate or invalid entity IDs.")

    for entity in entities:
        if not isinstance(entity, dict) or not entity.get("id") or not entity.get("name"):
            errors.append("ER model contains an invalid entity.")
            continue
        if not isinstance(entity.get("attributes"), list):
            errors.append(f"Entity {entity.get('name')} has invalid attributes.")

    for relation in relationships:
        if not isinstance(relation, dict):
            errors.append("ER model contains an invalid relationship.")
            continue
        if relation.get("from") not in entity_ids or relation.get("to") not in entity_ids:
            errors.append(f"Relationship {relation.get('id', '(unknown)')} references a missing entity.")

    return not errors, errors
