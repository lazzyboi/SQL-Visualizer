"""Upload all locally available SQL workspace databases to MongoDB Atlas GridFS.

Run from the project root after updating .env with the same MONGO_URI and
MONGO_DB used by the application:

    python scripts/migrate_session_databases_to_mongo.py

The script never deletes local SQLite files. If a remote copy already exists,
it restores that copy; if not, it uploads the existing local database or
creates the workspace's normal empty/seeded database first.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from config import MONGO_URI  # noqa: E402
from services.auth import admin_list_users  # noqa: E402
from services.sessions import (  # noqa: E402
    ensure_default_workspace,
    ensure_workspace_database,
    init_workspace_store,
    list_workspaces,
)
from services.session_db_store import session_database_store_status  # noqa: E402


def main() -> int:
    if not MONGO_URI:
        print("ERROR: MONGO_URI is not configured. Set it in the project .env file first.")
        return 2

    status = session_database_store_status()
    if not status.get("connected"):
        print(f"ERROR: {status.get('message', 'MongoDB connection failed.')}")
        return 3

    init_workspace_store()
    users = admin_list_users(limit=10000)
    uploaded_or_verified = 0
    total = 0
    failures: list[str] = []

    for user in users:
        user_id = str(user.get("id") or "")
        if not user_id:
            continue
        try:
            workspaces = list_workspaces(user_id)
            if not workspaces:
                # Ensure the legacy per-user sandbox is attached to a default
                # workspace so _ensure_db_filespace can migrate it as well.
                workspaces = [ensure_default_workspace(user_id)]
        except Exception as exc:
            failures.append(f"user {user_id}: workspace lookup failed ({type(exc).__name__})")
            continue

        for workspace in workspaces:
            total += 1
            try:
                ensure_workspace_database(user_id, workspace)
                uploaded_or_verified += 1
                print(f"OK  {workspace['name']}  [{workspace['id']}]")
            except Exception as exc:
                failures.append(
                    f"{workspace.get('name', workspace.get('id'))}: {type(exc).__name__}"
                )
                print(f"FAIL {workspace.get('name', workspace.get('id'))}: {type(exc).__name__}")

    print("\nMigration summary")
    print(f"Users checked: {len(users)}")
    print(f"Sessions checked: {total}")
    print(f"Sessions uploaded/verified: {uploaded_or_verified}")
    print(f"Failures: {len(failures)}")
    for failure in failures:
        print(f" - {failure}")

    if failures:
        return 4
    print("All local session databases are available in MongoDB Atlas GridFS.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
