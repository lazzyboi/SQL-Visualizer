from __future__ import annotations

import json
import re
from typing import Any

import requests

from config import AI_PROVIDER, AI_TIMEOUT, OLLAMA_MODEL, OLLAMA_URL


def _relationship_text(relation: dict[str, Any], entities: dict[str, dict[str, Any]]) -> str:
    source = entities.get(str(relation.get("from", "")), {})
    target = entities.get(str(relation.get("to", "")), {})
    source_name = source.get("name", relation.get("from", "unknown"))
    target_name = target.get("name", relation.get("to", "unknown"))
    cardinality = relation.get("cardinality", "unknown")
    participation = relation.get("participation") or {}
    left_participation = participation.get("from", "unknown")
    right_participation = participation.get("to", "unknown")
    attrs_from = ", ".join(relation.get("from_attributes") or []) or "the primary-side key"
    attrs_to = ", ".join(relation.get("to_attributes") or []) or "the foreign-key side"

    text = (
        f"{relation.get('name', source_name + ' to ' + target_name)} is {cardinality}. "
        f"It connects {source_name} ({left_participation} participation) to "
        f"{target_name} ({right_participation} participation). "
        f"The relationship uses {attrs_from} on the parent side and {attrs_to} on the child side."
    )
    if relation.get("identifying"):
        text += " It is an identifying relationship because the child foreign key participates in the child primary key."
    if relation.get("inferred_from_query"):
        text += " This relationship was inferred from the current JOIN and is not declared as a database foreign-key constraint."
    return text


def _entity_text(entity: dict[str, Any]) -> str:
    attributes = entity.get("attributes") or []
    if not attributes:
        return f"{entity.get('name', 'Entity')} has no exposed attributes."

    details = []
    for attribute in attributes:
        key = attribute.get("key") or ""
        flags = []
        if key:
            flags.append(key)
        if attribute.get("foreign_key"):
            flags.append("FK") if "FK" not in flags and key != "FK" else None
        if attribute.get("nullable") is False:
            flags.append("NOT NULL")
        if attribute.get("query_relevant"):
            flags.append("used by query")
        suffix = f" ({', '.join(flags)})" if flags else ""
        details.append(f"{attribute.get('name')} is {attribute.get('type', 'TEXT')}{suffix}")

    primary_key = entity.get("primary_key") or []
    primary_text = ", ".join(primary_key) if primary_key else "no primary key"
    unique_keys = entity.get("unique_keys") or []
    unique_text = "; ".join(", ".join(key) for key in unique_keys) if unique_keys else "none"
    return (
        f"{entity.get('name')} has primary key {primary_text}. Unique constraints/indexes: {unique_text}. "
        + " Attributes: "
        + "; ".join(details)
        + "."
    )


def _fallback(
    parsed: dict[str, Any],
    security: list[dict[str, Any]],
    performance: dict[str, Any],
    er_model: dict[str, Any] | None,
) -> dict[str, Any]:
    summary: list[str] = []
    teaching: list[str] = []
    suggestions: list[str] = []
    er_explanation: dict[str, Any] = {
        "overview": [],
        "entities": [],
        "relationships": [],
        "notation": [],
        "query_focus": [],
    }

    summary.extend(parsed.get("validation_errors", []))
    if parsed.get("select"):
        summary.append(f"The query selects {len(parsed['select'])} expression(s).")
    if parsed.get("from"):
        summary.append(f"The base table is `{parsed['from']}`.")
    if parsed.get("joins"):
        summary.append(f"The query contains {len(parsed['joins'])} JOIN operation(s).")
        teaching.append("A JOIN combines rows from related sources using a matching condition.")
    if parsed.get("where"):
        teaching.append("WHERE filters rows before the result is returned.")
    if "*" in parsed.get("select", []):
        summary.append("`SELECT *` requests every available column from the selected source tables.")
        suggestions.append("Select only the columns the application actually needs.")
    if security:
        summary.append("The security scanner found one or more patterns that should be reviewed before executing SQL with untrusted input.")
        suggestions.append("Use parameterized queries when application code inserts user-provided values into SQL.")
    if performance.get("available"):
        summary.extend(performance.get("insights", [])[:3])
    elif performance.get("message"):
        summary.append(performance["message"])
    teaching.append("The Performance tab shows SQLite's actual execution plan rather than a made-up cost score.")

    entities = {
        str(entity.get("id")): entity
        for entity in (er_model or {}).get("entities", [])
        if isinstance(entity, dict) and entity.get("id")
    }
    relationships = [
        relation
        for relation in (er_model or {}).get("relationships", [])
        if isinstance(relation, dict)
    ]

    if entities:
        er_explanation["overview"].append(
            f"The canonical ER model contains {len(entities)} entity/entities and {len(relationships)} relationship(s). "
            "The database schema is the source of truth; the current SQL only highlights the parts used by this query."
        )
        for entity in entities.values():
            er_explanation["entities"].append({
                "name": entity.get("name"),
                "text": _entity_text(entity),
                "query_relevant": bool(entity.get("query_relevant")),
            })
        for relation in relationships:
            er_explanation["relationships"].append({
                "name": relation.get("name"),
                "text": _relationship_text(relation, entities),
                "cardinality": relation.get("cardinality"),
                "query_relevant": bool(relation.get("query_relevant")),
            })

        notation = (er_model or {}).get("notation") or {}
        style = notation.get("style", "chen")
        er_explanation["notation"] = [
            f"Notation style: {style.title()} ER notation.",
            "Rectangle = entity.",
            "Oval = attribute.",
            "Diamond = relationship.",
            "Underlined attribute = primary key.",
            "Double rectangle = weak entity when the model marks an entity as weak.",
            "Double diamond = identifying relationship when the model marks a relationship as identifying.",
            "Cardinality labels such as 1:1 and 1:N describe how many instances can participate in a relationship.",
            "Participation indicates whether participation is partial or total based on the schema metadata.",
        ]

        focus = (er_model or {}).get("query_focus") or {}
        if focus.get("tables"):
            er_explanation["query_focus"].append(
                "Current query-highlighted entities: " + ", ".join(focus.get("tables", [])) + "."
            )
        if focus.get("attributes"):
            er_explanation["query_focus"].append(
                "Current query-highlighted attributes: " + ", ".join(focus.get("attributes", [])) + "."
            )
        if focus.get("relationships"):
            er_explanation["query_focus"].append(
                f"Current query touches {len(focus.get('relationships', []))} relationship(s) in the ER model."
            )

    return {
        "provider": "fallback",
        "summary": summary,
        "teaching": teaching,
        "suggestions": suggestions,
        "er_explanation": er_explanation,
        "raw": None,
    }


def _prompt(
    parsed: dict[str, Any],
    security: list[dict[str, Any]],
    performance: dict[str, Any],
    er_model: dict[str, Any] | None,
) -> str:
    return f"""You are a SQL tutor inside a web application.
Explain the SQL accurately and teach the user's database structure.
Never invent schema details. The canonical ER model is authoritative for entities, attributes, data types, keys, foreign keys, relationships, cardinality, participation, and ER notation.
The SQL query is only used to identify what should be highlighted.
Return valid JSON with keys:
summary (array of strings),
teaching (array of strings),
suggestions (array of strings),
er_explanation (object with overview, entities, relationships, notation, query_focus arrays).
For every entity, explain its attributes and their data types and key roles. For every relationship, explain endpoints, cardinality, participation and why it exists. Explain the ER notation actually used by the model. Do not claim a constraint exists unless the ER model contains it.

Parsed query:
{json.dumps(parsed, indent=2, default=str)}

Canonical ER model:
{json.dumps(er_model or {}, indent=2, default=str)}

Security scan:
{json.dumps(security, indent=2, default=str)}

Performance plan:
{json.dumps(performance, indent=2, default=str)}
"""


def _ollama(
    parsed: dict[str, Any],
    security: list[dict[str, Any]],
    performance: dict[str, Any],
    er_model: dict[str, Any] | None,
) -> dict[str, Any] | None:
    try:
        response = requests.post(
            OLLAMA_URL,
            json={
                "model": OLLAMA_MODEL,
                "prompt": _prompt(parsed, security, performance, er_model),
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
            "er_explanation": data.get("er_explanation", {}),
            "raw": raw,
        }
    except Exception:
        return None


def analyze(
    parsed: dict[str, Any],
    security: list[dict[str, Any]] | None = None,
    performance: dict[str, Any] | None = None,
    er_model: dict[str, Any] | None = None,
) -> dict[str, Any]:
    security = security or []
    performance = performance or {"available": False, "plan": [], "insights": []}
    if AI_PROVIDER == "ollama":
        generated = _ollama(parsed, security, performance, er_model)
        if generated:
            return generated
    return _fallback(parsed, security, performance, er_model)


def _find_entity(er_model: dict[str, Any], name: str) -> dict[str, Any] | None:
    wanted = (name or '').strip().lower()
    for entity in er_model.get('entities', []):
        if str(entity.get('name', '')).lower() == wanted or str(entity.get('id', '')).lower() == wanted:
            return entity
    return None


def _question_highlight(question: str, er_model: dict[str, Any], selected: dict[str, Any] | None = None) -> dict[str, list[str]]:
    q = (question or '').lower()
    highlight = {'entities': [], 'relationships': [], 'attributes': []}

    if selected:
        if selected.get('type') == 'entity' and selected.get('id'):
            highlight['entities'].append(str(selected['id']))
        elif selected.get('type') == 'relationship' and selected.get('id'):
            highlight['relationships'].append(str(selected['id']))
        elif selected.get('type') == 'attribute' and selected.get('id'):
            highlight['attributes'].append(str(selected['id']))

    for entity in er_model.get('entities', []):
        name = str(entity.get('name', ''))
        if name and name.lower() in q:
            highlight['entities'].append(str(entity.get('id', name.lower())))
            for attr in entity.get('attributes', []):
                attr_name = str(attr.get('name', ''))
                qualified = f"{name}.{attr_name}".lower()
                if qualified in q or (len(attr_name) > 2 and re.search(rf"\b{re.escape(attr_name.lower())}\b", q)):
                    highlight['attributes'].append(str(attr.get('id', '')))

    for relation in er_model.get('relationships', []):
        name = str(relation.get('name', ''))
        simple = name.replace(' → ', ' ')
        if name.lower() in q or simple.lower() in q:
            highlight['relationships'].append(str(relation.get('id', '')))

    return {k: list(dict.fromkeys(v)) for k, v in highlight.items()}


def _fallback_question(question: str, er_model: dict[str, Any], parsed: dict[str, Any] | None = None, selected: dict[str, Any] | None = None) -> dict[str, Any]:
    q = (question or '').strip()
    low = q.lower()
    entities = er_model.get('entities', [])
    relationships = er_model.get('relationships', [])
    highlight = _question_highlight(q, er_model, selected)

    selected_entity = None
    selected_attribute = None
    selected_relation = None
    if selected:
        if selected.get('type') == 'entity':
            selected_entity = next((e for e in entities if e.get('id') == selected.get('id')), None)
        elif selected.get('type') == 'attribute':
            for e in entities:
                for a in e.get('attributes', []):
                    if a.get('id') == selected.get('id'):
                        selected_entity, selected_attribute = e, a
                        break
        elif selected.get('type') == 'relationship':
            selected_relation = next((r for r in relationships if r.get('id') == selected.get('id')), None)

    answer = ''

    if selected_attribute:
        a = selected_attribute
        e = selected_entity
        answer = f"{e['name']}.{a['name']} is {a.get('type', 'TEXT')}"
        if a.get('key'):
            answer += f" and its key role is {a['key']}"
        if a.get('foreign_key'):
            answer += " and it is a foreign key"
        if a.get('references'):
            answer += f" referencing {a['references']['table']}.{a['references']['column']}"
        answer += "."
        if a.get('nullable') is False:
            answer += " It is NOT NULL."
        else:
            answer += " It is nullable."
    elif selected_relation:
        r = selected_relation
        answer = _relationship_text(r, {str(e.get('id')): e for e in entities})
    elif selected_entity:
        e = selected_entity
        pk = ', '.join(e.get('primary_key') or []) or 'none declared'
        attrs = ', '.join(f"{a.get('name')} ({a.get('type', 'TEXT')})" for a in e.get('attributes', [])) or 'none'
        answer = f"{e.get('name')} is an entity. Primary key: {pk}. Attributes: {attrs}."
    elif 'primary key' in low or re.search(r'\bpk\b', low):
        answer = "A primary key uniquely identifies each row in an entity. In this ER diagram it is represented by an underlined attribute."
    elif 'foreign key' in low or re.search(r'\bfk\b', low):
        answer = "A foreign key stores a value that references a key in another entity. It is what creates a declared relationship between the two tables."
    elif 'cardinality' in low or '1:n' in low or 'one-to-many' in low or 'one to many' in low:
        if relationships:
            parts = [f"{r.get('name')}: {r.get('cardinality')}" for r in relationships[:6]]
            answer = "Cardinality describes how many instances can participate. In this schema: " + '; '.join(parts) + "."
        else:
            answer = "No relationships are currently available in the ER model."
    elif 'datatype' in low or 'data type' in low or 'type of' in low:
        pairs = []
        for e in entities:
            for a in e.get('attributes', []):
                pairs.append(f"{e['name']}.{a['name']} = {a.get('type', 'TEXT')}")
        answer = "The schema data types are: " + '; '.join(pairs[:20]) + ('.' if pairs else ' No attributes are available.')
    elif 'relationship' in low or 'relation' in low or 'why does' in low:
        answer = "Relationships show how entities are connected through keys. The diamonds on the diagram represent them, and the cardinality tells you how many rows can participate."
    elif 'diagram' in low or 'er model' in low or 'explain my database' in low or 'explain er' in low:
        answer = f"This database has {len(entities)} entities and {len(relationships)} relationships. The rectangles are entities, ovals are attributes, and diamonds are relationships. Primary keys are underlined. I can explain any element you click on next."
    else:
        focus = (er_model.get('query_focus') or {}).get('tables') or []
        if focus:
            answer = f"Your current query focuses on {', '.join(focus)}. Ask me about a table, column, key, relationship, cardinality, datatype, JOIN, or the whole ER diagram."
        else:
            answer = "Ask about a table, attribute, key, foreign key, datatype, relationship, cardinality, JOIN, or the complete ER diagram."

    suggestions = []
    if selected_entity:
        suggestions = [
            f"What is the primary key of {selected_entity['name']}?",
            f"Which tables are related to {selected_entity['name']}?",
            f"Explain every attribute in {selected_entity['name']}.",
        ]
    elif selected_attribute:
        suggestions = [
            f"Why is {selected_entity['name']}.{selected_attribute['name']} this datatype?",
            "Is this attribute a PK or FK?",
            "How does this attribute participate in a relationship?",
        ]
    elif selected_relation:
        suggestions = [
            f"Why is this relationship {selected_relation.get('cardinality', 'this cardinality')}?",
            "What does the participation mean here?",
            "Which SQL JOIN uses this relationship?",
        ]
    else:
        suggestions = [
            "Explain the complete ER diagram.",
            "What are the primary and foreign keys?",
            "Why are the relationships 1:N?",
        ]

    return {
        'provider': 'fallback',
        'answer': answer,
        'suggestions': suggestions,
        'highlight': highlight,
        'context': {
            'entity_count': len(entities),
            'relationship_count': len(relationships),
        },
    }


def _question_prompt(question: str, er_model: dict[str, Any], parsed: dict[str, Any] | None, selected: dict[str, Any] | None) -> str:
    return f"""You are the interactive SQL Visualizer tutor.
Answer the user's question using ONLY the supplied canonical ER model and query context.
Explain clearly and pedagogically. When useful, refer to the ER diagram notation.
Return valid JSON with keys: answer (string), suggestions (array of strings), highlight (object with entities, relationships, attributes arrays).
Never invent tables, columns, keys, constraints or relationships.

QUESTION:
{question}

SELECTED ER ELEMENT:
{json.dumps(selected or {}, indent=2, default=str)}

PARSED QUERY:
{json.dumps(parsed or {}, indent=2, default=str)}

CANONICAL ER MODEL:
{json.dumps(er_model, indent=2, default=str)}
"""


def answer_question(question: str, er_model: dict[str, Any], parsed: dict[str, Any] | None = None, selected: dict[str, Any] | None = None) -> dict[str, Any]:
    question = (question or '').strip()
    if not question:
        return _fallback_question('', er_model, parsed, selected)

    if AI_PROVIDER == 'ollama':
        try:
            response = requests.post(
                OLLAMA_URL,
                json={
                    'model': OLLAMA_MODEL,
                    'prompt': _question_prompt(question, er_model, parsed, selected),
                    'stream': False,
                    'format': 'json',
                },
                timeout=AI_TIMEOUT,
            )
            response.raise_for_status()
            raw = response.json().get('response', '')
            data = json.loads(raw)
            data.setdefault('suggestions', [])
            data.setdefault('highlight', _question_highlight(question, er_model, selected))
            data['provider'] = f'ollama:{OLLAMA_MODEL}'
            return data
        except Exception:
            pass

    return _fallback_question(question, er_model, parsed, selected)
