# SQL Visualizer

A web-based SQL learning platform with per-user accounts, private SQLite sandbox databases, query history, ER state restoration, security checks, real SQLite query plans, an AI Tutor, and an admin database console.

## Local run

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python app.py
```

Open `http://127.0.0.1:10000`.

For local development, MongoDB may be left empty and the app will use the SQLite/JSON fallback. For production, MongoDB is required.

## MongoDB Atlas production setup

Set these environment variables in Render:

- `MONGO_URI`: Atlas connection string for an application database user
- `MONGO_DB`: application database name, for example `sqlviz`
- `MONGO_USERS_COLLECTION`: `users`
- `MONGO_HISTORY_COLLECTION`: `query_history`
- `ADMIN_EMAILS`: comma-separated emails that should receive the admin role when they register
- `SECRET_KEY`: long random Flask secret

Do not commit the real `MONGO_URI` or `SECRET_KEY` to Git. Keep them in Render environment secrets.

## Render

The repository includes `render.yaml`.

Build command:

```text
pip install -r requirements.txt
```

Start command:

```text
gunicorn --workers 2 --threads 4 --timeout 120 --bind 0.0.0.0:$PORT app:app
```

Health check:

```text
/healthz
```

Render's default filesystem is ephemeral, so this project uses a persistent disk mounted at `/var/data` for the per-user SQLite sandbox databases. Render documents that services with persistent disks cannot be scaled to multiple instances, so the blueprint intentionally uses one instance. MongoDB Atlas remains the source of truth for accounts and query history.

## Data model

MongoDB collections:

- `users`: account id, email, password hash, role, created timestamp
- `query_history`: per-user SQL, analysis, performance, security, AI output, result output, and ER state

Password values are never stored in plaintext. The admin console can show the stored password hash to demonstrate the database design.

## Admin console

Register using an email listed in `ADMIN_EMAILS`. The **Admin DB** page lets the admin inspect:

- MongoDB connection/collection status
- registered users and password hashes
- stored query history
- per-user SQLite sandbox tables and sample rows

The MongoDB connection URI itself is deliberately never printed by the application. It remains a deployment secret in Render.

## Check the Atlas cluster locally

Put your real Atlas URI in `.env` as `MONGO_URI`. Do not paste that URI into source control or into chat.

Then run:

```powershell
python scripts/check_mongo.py
```

The script prints the cluster host, MongoDB username, database, collection names, and ping status. It never prints the MongoDB password.
