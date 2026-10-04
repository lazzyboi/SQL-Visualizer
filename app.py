from datetime import timedelta
from functools import wraps
import secrets

from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

from config import (
    APP_ENV,
    PORT,
    SECRET_KEY,
    SESSION_COOKIE_SECURE,
    SESSION_LIFETIME_SECONDS,
)
from services.admin import sandbox_data
from services.ai import analyze
from services.auth import (
    admin_list_users,
    authenticate_user,
    database_schema,
    get_user,
    get_user_db_path,
    init_auth_db,
    is_admin,
    mongo_status as auth_mongo_status,
    register_user,
    seed_user_database,
)
from services.output import execute_read_only
from services.parser import parse_query
from services.performance import explain_query
from services.security import scan
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


def current_user():
    user_id = session.get("user_id")
    return get_user(user_id) if user_id else None


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


@app.route("/healthz")
def healthz():
    auth_status = auth_mongo_status()
    history_status = history_mongo_status()
    mongo_ok = (not auth_status["configured"] and APP_ENV != "production") or (
        auth_status["configured"] and auth_status["connected"] and history_status["connected"]
    )
    status_code = 200 if mongo_ok else 503
    return jsonify({
        "status": "ok" if mongo_ok else "degraded",
        "environment": APP_ENV,
        "mongo": {
            "configured": auth_status["configured"],
            "connected": auth_status["connected"] and history_status["connected"],
        },
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
    seed_user_database(user["id"])
    return render_template("index.html", user=user, is_admin=is_admin(user))


@app.route("/admin")
@admin_required
def admin_page():
    return render_template("admin.html", user=current_user())


@app.route("/api/csrf")
@login_required
def csrf_api():
    return jsonify({"token": ensure_csrf_token()})


@app.route("/analyze", methods=["POST"])
@login_required
def analyze_query():
    if not csrf_valid():
        return jsonify({"error": "Invalid CSRF token"}), 400
    user = current_user()
    body = request.get_json(silent=True) or {}
    query = (body.get("query") or "").strip()
    previous_er_state = body.get("er_state") or {}
    if not query:
        return jsonify({"error": "Query is required"}), 400

    parsed = parse_query(query)
    security = scan(query)
    db_path = get_user_db_path(user["id"])
    performance = explain_query(query, db_path)
    ai = analyze(parsed, security, performance)
    output = execute_read_only(query, db_path)

    er_state = previous_er_state if previous_er_state and previous_er_state.get("nodes") else {
        "nodes": [],
        "links": [],
        "zoom": 1,
        "translate": [0, 0],
    }

    result = {
        "query": query,
        "parsed": parsed,
        "ai": ai,
        "performance": performance,
        "security": security,
        "output": output,
        "er_state": er_state,
    }

    try:
        history_item = create_history(user["id"], result)
    except StorageUnavailable as exc:
        return jsonify({"error": str(exc)}), 503
    return jsonify(history_item)


@app.route("/history", methods=["GET"])
@login_required
def history():
    user = current_user()
    limit = min(max(request.args.get("limit", default=100, type=int), 1), 200)
    try:
        return jsonify(list_history(user["id"], limit))
    except StorageUnavailable as exc:
        return jsonify({"error": str(exc)}), 503


@app.route("/history/<record_id>", methods=["GET"])
@login_required
def history_item(record_id: str):
    user = current_user()
    try:
        item = get_history(user["id"], record_id)
    except StorageUnavailable as exc:
        return jsonify({"error": str(exc)}), 503
    if not item:
        return jsonify({"error": "History item not found"}), 404
    return jsonify(item)


@app.route("/history/<record_id>/state", methods=["POST"])
@login_required
def update_history_state(record_id: str):
    if not csrf_valid():
        return jsonify({"error": "Invalid CSRF token"}), 400
    user = current_user()
    body = request.get_json(silent=True) or {}
    er_state = body.get("er_state")
    if not isinstance(er_state, dict):
        return jsonify({"error": "er_state must be an object"}), 400
    try:
        updated = update_history(user["id"], record_id, {"er_state": er_state})
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
    return jsonify(database_schema(user["id"]))


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
