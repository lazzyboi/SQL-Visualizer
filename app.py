from datetime import timedelta
from functools import wraps
import math
import secrets

from flask import Flask, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

from config import (
    APP_ENV,
    PORT,
    SECRET_KEY,
    SESSION_COOKIE_SECURE,
    SESSION_LIFETIME_SECONDS,
)
from services.admin import sandbox_data
from services.ai import analyze, answer_question
from services.auth import (
    admin_list_users,
    authenticate_user,
    clear_session_database_data,
    clear_user_database_data,
    database_schema,
    database_schema_for_session,
    get_session_db_path,
    get_user,
    get_user_db_path,
    init_auth_db,
    is_admin,
    mongo_status as auth_mongo_status,
    register_user,
    seed_user_database,
)
from services.er_model import SCHEMA_VERSION, ER_MODEL_VERSION, build_er_model, validate_er_model
from services.output import execute_read_only
from services.parser import parse_query
from services.performance import explain_query
from services.security import scan
from services.session_db_store import (
    SessionDatabaseStoreError,
    acquire_session_database_lock,
    persist_session_database,
    session_database_store_status,
)
from services.storage import (
    StorageUnavailable,
    admin_list_queries,
    create_history,
    get_history,
    list_history,
    mongo_collections,
    mongo_status as history_mongo_status,
    update_history,
)
from services.sessions import (
    create_workspace,
    delete_workspace,
    ensure_default_workspace,
    get_workspace,
    init_workspace_store,
    list_workspaces,
    mark_workspace_cleared,
    rename_workspace,
    touch_workspace,
    ensure_workspace_database,
)

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=SESSION_COOKIE_SECURE,
    PERMANENT_SESSION_LIFETIME=timedelta(seconds=SESSION_LIFETIME_SECONDS),
)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

init_auth_db()
init_workspace_store()


def current_user():
    user_id = session.get("user_id")
    return get_user(user_id) if user_id else None


def current_workspace(user_id: str, ensure: bool = True) -> dict | None:
    """Return the active isolated learning workspace for the signed-in user."""
    workspace_id = session.get("workspace_id")
    workspace = get_workspace(user_id, workspace_id) if workspace_id else None
    if workspace is None and ensure:
        workspace = ensure_default_workspace(user_id)
        session["workspace_id"] = workspace["id"]
    return workspace


def select_workspace(user_id: str, workspace_id: str) -> dict | None:
    workspace = get_workspace(user_id, workspace_id)
    if workspace is not None:
        session["workspace_id"] = workspace["id"]
    return workspace


_SESSION_DB_ENDPOINTS = {
    "analyze_query",
    "history_item",
    "update_history_state",
    "db_schema",
    "clear_database",
    "canonical_er_model",
    "ai_ask",
}


@app.before_request
def restore_request_workspace_database():
    """Hydrate the selected session's SQLite file before any DB-backed route."""
    if request.endpoint not in _SESSION_DB_ENDPOINTS:
        return None
    user = current_user()
    if user is None:
        return None

    requested_session_id = ""
    if request.endpoint == "analyze_query":
        body = request.get_json(silent=True) or {}
        requested_session_id = str(body.get("session_id") or "").strip()
    elif request.endpoint == "history_item":
        requested_session_id = str(request.args.get("session_id") or "").strip()
    elif request.endpoint == "update_history_state":
        body = request.get_json(silent=True) or {}
        requested_session_id = str(body.get("session_id") or "").strip()

    workspace = (
        get_workspace(user["id"], requested_session_id)
        if requested_session_id
        else current_workspace(user["id"])
    )
    if workspace is None:
        return None  # The route itself returns the correct not-found response.

    lock = acquire_session_database_lock(user["id"], workspace["id"])
    try:
        db_path = ensure_workspace_database(user["id"], workspace)
        g.sqlviz_session_database = {
            "user_id": user["id"],
            "session_id": workspace["id"],
            "path": db_path,
            "lock": lock,
        }
    except SessionDatabaseStoreError as exc:
        lock.release()
        return jsonify({"error": str(exc)}), 503
    except Exception as exc:
        lock.release()
        app.logger.exception("Could not prepare the selected session database")
        return jsonify({"error": f"Unable to prepare session database: {type(exc).__name__}"}), 503
    return None


@app.after_request
def persist_request_workspace_database(response):
    """Write changed SQLite files to Atlas before returning a successful response."""
    context = getattr(g, "sqlviz_session_database", None)
    if context is not None:
        try:
            # Persist even when a later operation returned an error: an SQLite
            # operation may already have committed before another step failed.
            persist_session_database(
                context["user_id"], context["session_id"], context["path"]
            )
        except SessionDatabaseStoreError as exc:
            app.logger.exception("Could not persist the selected session database")
            response.status_code = 503
            response.set_data(jsonify({"error": str(exc)}).get_data())
            response.content_type = "application/json"
        finally:
            context["lock"].release()
    return response


def ensure_csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def csrf_valid() -> bool:
    token = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
    return bool(token and secrets.compare_digest(token, session.get("csrf_token", "")))


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if user is None:
            return redirect(url_for("login"))
        if not is_admin(user):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Administrator access required"}), 403
            return "Forbidden", 403
        return view(*args, **kwargs)
    return wrapped


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_user() is None:
            if request.path.startswith("/api/") or request.path in {"/analyze"}:
                return jsonify({"error": "Authentication required"}), 401
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


@app.after_request
def security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if APP_ENV == "production":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


def _default_er_layout() -> dict:
    return {
        "version": 3,
        "mode": "full",
        "nodes": [],
        "zoom": 1,
        "translate": [0, 0],
    }


def _coerce_er_layout(value) -> dict:
    if not isinstance(value, dict):
        return _default_er_layout()

    source_version = value.get("version", 0)
    try:
        source_version = int(source_version)
    except (TypeError, ValueError):
        source_version = 0
    nodes = value.get("nodes") if source_version >= 3 else []
    if not isinstance(nodes, list):
        nodes = []

    clean_nodes = []
    seen = set()
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_id = str(node.get("id") or "").strip().lower()
        x = node.get("x")
        y = node.get("y")
        if not node_id:
            continue
        try:
            x = float(x)
            y = float(y)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(x) or not math.isfinite(y) or node_id in seen:
            continue
        seen.add(node_id)
        clean_nodes.append({"id": node_id, "x": x, "y": y})

    zoom = value.get("zoom", 1)
    try:
        zoom = float(zoom)
    except (TypeError, ValueError):
        zoom = 1.0
    if not math.isfinite(zoom) or zoom <= 0:
        zoom = 1.0

    translate = value.get("translate", [0, 0])
    if not isinstance(translate, (list, tuple)) or len(translate) != 2:
        translate = [0, 0]
    try:
        tx, ty = float(translate[0]), float(translate[1])
    except (TypeError, ValueError):
        tx, ty = 0.0, 0.0
    if not math.isfinite(tx) or not math.isfinite(ty):
        tx, ty = 0.0, 0.0

    return {
        "version": 3,
        "mode": str(value.get("mode") or "full"),
        "nodes": clean_nodes,
        "zoom": zoom,
        "translate": [tx, ty],
    }


def _validate_er_layout(layout: dict, er_model: dict) -> tuple[bool, list[str]]:
    errors = []
    if not isinstance(layout, dict):
        return False, ["ER layout is not an object."]

    valid_ids = {str(entity.get("id")) for entity in er_model.get("entities", []) if isinstance(entity, dict)}
    seen = set()
    for node in layout.get("nodes", []):
        if not isinstance(node, dict):
            errors.append("ER layout contains an invalid node.")
            continue
        node_id = str(node.get("id") or "").lower()
        if node_id not in valid_ids:
            errors.append(f"ER layout references missing entity: {node_id}.")
        if node_id in seen:
            errors.append(f"ER layout contains duplicate entity: {node_id}.")
        seen.add(node_id)
        for axis in ("x", "y"):
            try:
                value = float(node.get(axis))
            except (TypeError, ValueError):
                errors.append(f"ER layout node {node_id} has invalid {axis}.")
                continue
            if not math.isfinite(value):
                errors.append(f"ER layout node {node_id} has non-finite {axis}.")

    return not errors, errors


def _build_current_er_model(user_id: str, session_id: str, query: str, parsed: dict, data_state: str = "seeded") -> dict:
    db_path = get_session_db_path(user_id, session_id)
    from services.auth import initialize_session_database
    initialize_session_database(db_path, seed_demo=data_state != "cleared")
    return build_er_model(db_path, parsed, query)


def _repair_history_item(user_id: str, session_id: str, item: dict, data_state: str = "seeded") -> dict:
    """Normalize old/new history into the canonical ER contract.

    Old entries may only contain parsed.er / er_state. They are migrated on read
    using the current database schema. Invalid layouts are discarded so the UI
    can regenerate a safe layout from the canonical ER model.
    """
    item = dict(item)
    query = str(item.get("query") or "")
    parsed = item.get("parsed")
    if not isinstance(parsed, dict):
        parsed = parse_query(query)
        item["parsed"] = parsed

    er_model = item.get("er_model")
    model_valid, _ = validate_er_model(er_model)
    repaired_model = False
    if not model_valid:
        try:
            er_model = _build_current_er_model(user_id, session_id, query, parsed, data_state)
            item["er_model"] = er_model
            item["schema_version"] = SCHEMA_VERSION
            item["er_model_version"] = ER_MODEL_VERSION
            repaired_model = True
        except Exception:
            er_model = None

    layout_source = item.get("er_layout")
    if layout_source is None:
        layout_source = item.get("er_state")
    layout = _coerce_er_layout(layout_source)
    layout_valid = bool(er_model) and _validate_er_layout(layout, er_model)[0]

    recovery_message = item.get("er_recovery_message")
    if er_model and not layout_valid:
        layout = _default_er_layout()
        recovery_message = "Saved layout unavailable — diagram regenerated."
    elif repaired_model and not recovery_message:
        recovery_message = "Saved ER model migrated to the current schema format."

    if er_model:
        item["er_model"] = er_model
        item["schema_version"] = SCHEMA_VERSION
        item["er_model_version"] = ER_MODEL_VERSION
    item["er_layout"] = layout
    # Backward compatibility for the current frontend while it is being rebuilt.
    item["er_state"] = layout
    if recovery_message:
        item["er_recovery_message"] = recovery_message

    if recovery_message or repaired_model or "er_layout" not in item:
        try:
            persisted = {
                "er_model": item.get("er_model"),
                "er_layout": item.get("er_layout"),
                "er_state": item.get("er_layout"),
                "schema_version": item.get("schema_version"),
                "er_model_version": item.get("er_model_version"),
                "er_recovery_message": item.get("er_recovery_message"),
            }
            update_history(user_id, item.get("id"), persisted, session_id=session_id)
        except Exception:
            pass

    return item


@app.route("/healthz")
def healthz():
    auth_status = auth_mongo_status()
    history_status = history_mongo_status()
    session_db_status = session_database_store_status()
    mongo_ok = (not auth_status["configured"] and APP_ENV != "production") or (
        auth_status["configured"]
        and auth_status["connected"]
        and history_status["connected"]
        and session_db_status["connected"]
    )
    status_code = 200 if mongo_ok else 503
    return jsonify({
        "status": "ok" if mongo_ok else "degraded",
        "environment": APP_ENV,
        "mongo": {
            "configured": auth_status["configured"],
            "connected": auth_status["connected"] and history_status["connected"],
        },
        "session_database_storage": session_db_status,
    }), status_code


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user():
        return redirect(url_for("home"))
    ensure_csrf_token()
    error = None
    if request.method == "POST":
        if not csrf_valid():
            error = "Your session token expired. Refresh and try again."
        else:
            user = authenticate_user(request.form.get("email", ""), request.form.get("password", ""))
            if user:
                session.clear()
                session.permanent = True
                session["user_id"] = user["id"]
                ensure_csrf_token()
                return redirect(url_for("home"))
            error = "Email or password is incorrect."
    return render_template("login.html", error=error, csrf_token=ensure_csrf_token())


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user():
        return redirect(url_for("home"))
    ensure_csrf_token()
    error = None
    if request.method == "POST":
        if not csrf_valid():
            error = "Your session token expired. Refresh and try again."
        else:
            password = request.form.get("password", "")
            confirmation = request.form.get("confirmation", "")
            if password != confirmation:
                error = "Passwords do not match."
            else:
                try:
                    user = register_user(request.form.get("email", ""), password)
                    session.clear()
                    session.permanent = True
                    session["user_id"] = user["id"]
                    ensure_csrf_token()
                    return redirect(url_for("home"))
                except ValueError as exc:
                    error = str(exc)
    return render_template("register.html", error=error, csrf_token=ensure_csrf_token())


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def home():
    user = current_user()
    workspace = current_workspace(user["id"])
    return render_template("index.html", user=user, is_admin=is_admin(user), workspace=workspace)


@app.route("/admin")
@admin_required
def admin_page():
    return render_template("admin.html", user=current_user())


@app.route("/api/csrf")
@login_required
def csrf_api():
    return jsonify({"token": ensure_csrf_token()})


@app.route("/api/sessions", methods=["GET"])
@login_required
def sessions_api():
    user = current_user()
    current = current_workspace(user["id"])
    try:
        items = list_workspaces(user["id"])
        return jsonify({"sessions": items, "current": current})
    except Exception as exc:
        return jsonify({"error": f"Unable to load workspaces: {type(exc).__name__}"}), 503


@app.route("/api/sessions", methods=["POST"])
@login_required
def create_session_api():
    if not csrf_valid():
        return jsonify({"error": "Invalid CSRF token"}), 400
    user = current_user()
    body = request.get_json(silent=True) or {}
    name = str(body.get("name") or "New Workspace").strip()
    try:
        workspace = create_workspace(user["id"], name=name)
        session["workspace_id"] = workspace["id"]
        return jsonify({"success": True, "session": workspace, "sessions": list_workspaces(user["id"])})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": f"Unable to create workspace: {type(exc).__name__}"}), 503


@app.route("/api/sessions/select", methods=["POST"])
@login_required
def select_session_api():
    if not csrf_valid():
        return jsonify({"error": "Invalid CSRF token"}), 400
    user = current_user()
    body = request.get_json(silent=True) or {}
    workspace_id = str(body.get("id") or "").strip()
    if not workspace_id:
        return jsonify({"error": "Workspace id is required"}), 400
    workspace = select_workspace(user["id"], workspace_id)
    if workspace is None:
        return jsonify({"error": "Workspace not found"}), 404
    touch_workspace(user["id"], workspace_id)
    return jsonify({"success": True, "session": get_workspace(user["id"], workspace_id)})


@app.route("/api/sessions/<session_id>", methods=["PATCH"])
@login_required
def rename_session_api(session_id: str):
    if not csrf_valid():
        return jsonify({"error": "Invalid CSRF token"}), 400
    user = current_user()
    if get_workspace(user["id"], session_id) is None:
        return jsonify({"error": "Workspace not found"}), 404
    body = request.get_json(silent=True) or {}
    try:
        workspace = rename_workspace(user["id"], session_id, str(body.get("name") or ""))
        return jsonify({"success": True, "session": workspace})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": f"Unable to rename workspace: {type(exc).__name__}"}), 503


@app.route("/api/sessions/<session_id>", methods=["DELETE"])
@login_required
def delete_session_api(session_id: str):
    if not csrf_valid():
        return jsonify({"error": "Invalid CSRF token"}), 400
    user = current_user()
    active_id = session.get("workspace_id")
    try:
        next_workspace = delete_workspace(user["id"], session_id)
        if active_id == session_id:
            session["workspace_id"] = next_workspace["id"]
            active_workspace_item = next_workspace
        else:
            active_workspace_item = get_workspace(user["id"], active_id) or next_workspace
        return jsonify({
            "success": True,
            "deleted_id": session_id,
            "session": active_workspace_item,
            "sessions": list_workspaces(user["id"]),
        })
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": f"Unable to delete workspace: {type(exc).__name__}"}), 503


@app.route("/analyze", methods=["POST"])
@login_required
def analyze_query():
    if not csrf_valid():
        return jsonify({"error": "Invalid CSRF token"}), 400

    user = current_user()
    body = request.get_json(silent=True) or {}
    requested_session_id = str(body.get("session_id") or "").strip()
    workspace = (
        get_workspace(user["id"], requested_session_id)
        if requested_session_id
        else current_workspace(user["id"])
    )
    if workspace is None:
        return jsonify({"error": "The selected session was not found."}), 404
    query = (body.get("query") or "").strip()
    if not query:
        return jsonify({"error": "Query is required"}), 400

    parsed = parse_query(query)
    security = scan(query)
    db_path = get_session_db_path(user["id"], workspace["id"])
    from services.auth import initialize_session_database
    initialize_session_database(db_path, seed_demo=workspace.get("data_state") != "cleared")
    performance = explain_query(query, db_path)

    try:
        er_model = build_er_model(db_path, parsed, query)
    except Exception as exc:
        er_model = {
            "schema_version": SCHEMA_VERSION,
            "er_model_version": ER_MODEL_VERSION,
            "notation": {},
            "entities": [],
            "relationships": [],
            "query_focus": {"tables": [], "attributes": [], "relationships": [], "sql": query},
            "error": f"ER model generation failed: {type(exc).__name__}",
        }

    ai = analyze(parsed, security, performance, er_model)
    output = execute_read_only(query, db_path)

    incoming_layout = body.get("er_layout")
    if incoming_layout is None:
        incoming_layout = body.get("er_state")
    er_layout = _coerce_er_layout(incoming_layout)
    layout_valid, _ = _validate_er_layout(er_layout, er_model)
    if not layout_valid:
        er_layout = _default_er_layout()

    result = {
        "query": query,
        "parsed": parsed,
        "ai": ai,
        "performance": performance,
        "security": security,
        "output": output,
        "er_model": er_model,
        "er_layout": er_layout,
        # Backward compatibility while frontend migration is in progress.
        "er_state": er_layout,
        "schema_version": SCHEMA_VERSION,
        "er_model_version": ER_MODEL_VERSION,
    }

    try:
        history_item = create_history(user["id"], result, session_id=workspace["id"])
        touch_workspace(user["id"], workspace["id"])
    except StorageUnavailable as exc:
        return jsonify({"error": str(exc)}), 503
    return jsonify(history_item)


@app.route("/history", methods=["GET"])
@login_required
def history():
    user = current_user()
    requested_session_id = str(request.args.get("session_id") or "").strip()
    workspace = (
        get_workspace(user["id"], requested_session_id)
        if requested_session_id
        else current_workspace(user["id"])
    )
    if workspace is None:
        return jsonify({"error": "The selected session was not found."}), 404
    limit = min(max(request.args.get("limit", default=100, type=int), 1), 200)
    try:
        return jsonify(list_history(user["id"], limit, session_id=workspace["id"]))
    except StorageUnavailable as exc:
        return jsonify({"error": str(exc)}), 503


@app.route("/history/<record_id>", methods=["GET"])
@login_required
def history_item(record_id: str):
    user = current_user()
    requested_session_id = str(request.args.get("session_id") or "").strip()
    workspace = (
        get_workspace(user["id"], requested_session_id)
        if requested_session_id
        else current_workspace(user["id"])
    )
    if workspace is None:
        return jsonify({"error": "The selected session was not found."}), 404
    try:
        item = get_history(user["id"], record_id, session_id=workspace["id"])
    except StorageUnavailable as exc:
        return jsonify({"error": str(exc)}), 503
    if not item:
        return jsonify({"error": "History item not found"}), 404
    return jsonify(_repair_history_item(user["id"], workspace["id"], item, workspace.get("data_state", "seeded")))


@app.route("/history/<record_id>/state", methods=["POST"])
@login_required
def update_history_state(record_id: str):
    if not csrf_valid():
        return jsonify({"error": "Invalid CSRF token"}), 400

    user = current_user()
    body = request.get_json(silent=True) or {}
    requested_session_id = str(body.get("session_id") or "").strip()
    workspace = (
        get_workspace(user["id"], requested_session_id)
        if requested_session_id
        else current_workspace(user["id"])
    )
    if workspace is None:
        return jsonify({"error": "The selected session was not found."}), 404
    raw_layout = body.get("er_layout")
    if raw_layout is None:
        raw_layout = body.get("er_state")
    if not isinstance(raw_layout, dict):
        return jsonify({"error": "er_layout must be an object"}), 400

    existing = get_history(user["id"], record_id, session_id=workspace["id"])
    if not existing:
        return jsonify({"error": "History item not found"}), 404

    er_model = existing.get("er_model")
    if not isinstance(er_model, dict):
        parsed = existing.get("parsed") or parse_query(str(existing.get("query") or ""))
        try:
            er_model = _build_current_er_model(user["id"], workspace["id"], str(existing.get("query") or ""), parsed, workspace.get("data_state", "seeded"))
        except Exception as exc:
            return jsonify({"error": f"Unable to validate ER layout: {type(exc).__name__}"}), 503

    layout = _coerce_er_layout(raw_layout)
    valid, errors = _validate_er_layout(layout, er_model)
    if not valid:
        return jsonify({"error": "Invalid ER layout", "details": errors}), 400

    try:
        updated = update_history(
            user["id"],
            record_id,
            {
                "er_layout": layout,
                "er_state": layout,
                "er_model": er_model,
                "schema_version": SCHEMA_VERSION,
                "er_model_version": ER_MODEL_VERSION,
            },
            session_id=workspace["id"],
        )
        touch_workspace(user["id"], workspace["id"])
    except StorageUnavailable as exc:
        return jsonify({"error": str(exc)}), 503
    if not updated:
        return jsonify({"error": "History item not found"}), 404
    return jsonify(updated)


@app.route("/api/me")
@login_required
def me():
    return jsonify(current_user())


@app.route("/api/database/schema")
@login_required
def db_schema():
    user = current_user()
    workspace = current_workspace(user["id"])
    try:
        from services.auth import initialize_session_database
        path = get_session_db_path(user["id"], workspace["id"])
        initialize_session_database(path, seed_demo=workspace.get("data_state") != "cleared")
        return jsonify(database_schema_for_session(user["id"], workspace["id"]))
    except Exception as exc:
        return jsonify({"error": f"Unable to load schema: {type(exc).__name__}"}), 503


@app.route("/api/database/clear", methods=["POST"])
@login_required
def clear_database():
    if not csrf_valid():
        return jsonify({"error": "Invalid CSRF token"}), 400
    user = current_user()
    workspace = current_workspace(user["id"])
    try:
        deleted = clear_session_database_data(user["id"], workspace["id"])
        mark_workspace_cleared(user["id"], workspace["id"])
        touch_workspace(user["id"], workspace["id"])
        return jsonify({
            "success": True,
            "message": "All rows in the current workspace were deleted.",
            "deleted": deleted,
            "total_rows": sum(deleted.values()),
            "workspace": get_workspace(user["id"], workspace["id"]),
        })
    except Exception as exc:
        return jsonify({"error": f"Unable to clear database: {type(exc).__name__}"}), 500


@app.route("/api/er-model")
@login_required
def canonical_er_model():
    user = current_user()
    workspace = current_workspace(user["id"])
    try:
        parsed = {"tables": [], "select": [], "where": "", "joins": []}
        model = _build_current_er_model(user["id"], workspace["id"], "", parsed, workspace.get("data_state", "seeded"))
        return jsonify(model)
    except Exception as exc:
        return jsonify({"error": f"Unable to build ER model: {type(exc).__name}"}), 503



@app.route("/api/ai/ask", methods=["POST"])
@login_required
def ai_ask():
    if not csrf_valid():
        return jsonify({"error": "Invalid CSRF token"}), 400

    user = current_user()
    workspace = current_workspace(user["id"])
    body = request.get_json(silent=True) or {}
    question = str(body.get("question") or "").strip()
    selected = body.get("selected") if isinstance(body.get("selected"), dict) else None
    query = str(body.get("query") or "").strip()

    if not question:
        return jsonify({"error": "Question is required"}), 400

    parsed = body.get("parsed")
    if not isinstance(parsed, dict):
        parsed = parse_query(query) if query else {"select": [], "from": "", "joins": [], "where": "", "tables": [], "validation_errors": []}

    er_model = body.get("er_model")
    valid, _ = validate_er_model(er_model)
    if not valid:
        try:
            db_path = get_session_db_path(user["id"], workspace["id"])
            from services.auth import initialize_session_database
            initialize_session_database(db_path, seed_demo=workspace.get("data_state") != "cleared")
            er_model = build_er_model(db_path, parsed, query)
        except Exception as exc:
            return jsonify({"error": f"Unable to build ER context: {type(exc).__name__}"}), 503

    result = answer_question(
        question,
        er_model,
        parsed,
        selected,
    )
    return jsonify(result)


@app.route("/api/admin/overview")
@admin_required
def admin_overview():
    try:
        users = admin_list_users(100)
        queries = admin_list_queries(100)
        collections = mongo_collections()
        auth_status = auth_mongo_status()
        history_status = history_mongo_status()
        return jsonify({
            "mongo": {
                "auth": auth_status,
                "history": history_status,
                "collections": collections,
            },
            "users": users,
            "queries": queries,
        })
    except StorageUnavailable as exc:
        return jsonify({"error": str(exc)}), 503


@app.route("/api/admin/sandbox/<user_id>")
@admin_required
def admin_sandbox(user_id: str):
    user = get_user(user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404
    schema = database_schema(user_id)
    previews = []
    for table in schema:
        try:
            previews.append(sandbox_data(user_id, table["name"], limit=25))
        except ValueError:
            continue
    return jsonify({"user": user, "schema": schema, "previews": previews, "database_path": get_user_db_path(user_id)})


@app.errorhandler(StorageUnavailable)
def storage_error(exc):
    return jsonify({"error": str(exc)}), 503


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=APP_ENV != "production")
