#!/usr/bin/env python3
"""Create a transactionally consistent SQLite backup; do not copy .db files directly."""
from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path


source = Path(os.environ.get("DATABASE_PATH", "/opt/rnimu-tgbot/data/app.db"))
destination_dir = Path(os.environ.get("DATABASE_BACKUP_DIR", "/var/backups/pathology-bot"))
retention_days = int(os.environ.get("DATABASE_BACKUP_RETENTION_DAYS", "14"))
if retention_days < 1:
    raise SystemExit("DATABASE_BACKUP_RETENTION_DAYS must be positive")
destination_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
destination = destination_dir / f"app-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.db"

if not source.is_file():
    raise SystemExit(f"Database does not exist: {source}")

with sqlite3.connect(source) as source_db, sqlite3.connect(destination) as destination_db:
    source_db.backup(destination_db)
os.chmod(destination, 0o600)

cutoff = datetime.now(UTC) - timedelta(days=retention_days)
for backup in destination_dir.glob("app-*.db"):
    if backup != destination and datetime.fromtimestamp(backup.stat().st_mtime, UTC) < cutoff:
        backup.unlink()
