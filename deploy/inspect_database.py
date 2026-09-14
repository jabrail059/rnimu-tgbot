#!/usr/bin/env python3
"""Read-only report for finding the SQLite file that contains subscriptions."""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv


project_root = Path(__file__).resolve().parent.parent
load_dotenv(project_root / ".env")
requested = [Path(value).expanduser() for value in sys.argv[1:]]
configured = Path(os.environ.get("DATABASE_PATH", "data/app.db")).expanduser()
backup_directory = Path(os.environ.get("DATABASE_BACKUP_DIR", "/var/backups/pathology-bot")).expanduser()
candidates = requested or [configured, project_root / "data/app.db", project_root / "app.db",
                            Path.cwd() / "data/app.db", Path.cwd() / "app.db",
                            *sorted(backup_directory.glob("app-*.db"), reverse=True)]
seen: set[Path] = set()

for candidate in candidates:
    path = (project_root / candidate if not candidate.is_absolute() else candidate).resolve()
    if path in seen:
        continue
    seen.add(path)
    if not path.is_file():
        print(f"MISSING  {path}")
        continue
    try:
        database = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "users" not in tables:
            print(f"NO USERS {path} ({path.stat().st_size} bytes)")
            database.close()
            continue
        user_columns = {row[1] for row in database.execute("PRAGMA table_info(users)")}
        users = database.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        active = database.execute("SELECT COUNT(*) FROM users WHERE julianday(subscription_end)>julianday('now')").fetchone()[0]
        succeeded = database.execute("SELECT COUNT(*) FROM payments WHERE status IN ('succeeded','success')").fetchone()[0] if "payments" in tables else 0
        print(f"DATABASE {path} ({path.stat().st_size} bytes): users={users}, active={active}, succeeded_payments={succeeded}")
        start_expression = "subscription_start" if "subscription_start" in user_columns else "NULL"
        for user_id, username, start, end in database.execute(
                f"SELECT user_id, COALESCE(username, ''), {start_expression}, subscription_end "
                "FROM users WHERE subscription_end IS NOT NULL ORDER BY subscription_end DESC"):
            print(f"  user={user_id} @{username or '-'} start={start or '-'} end={end}")
        database.close()
    except sqlite3.Error as exc:
        print(f"INVALID  {path}: {exc}")
