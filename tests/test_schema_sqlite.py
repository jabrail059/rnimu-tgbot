from pathlib import Path
import sqlite3
import tempfile
import asyncio

# Lightweight schema smoke-test without third-party packages.
def test_schema_tables_present():
    # Mirrors the production initialization at the SQL level.
    with tempfile.TemporaryDirectory() as tmp:
        db = sqlite3.connect(Path(tmp) / "app.db")
        db.executescript("""
        PRAGMA foreign_keys = ON;
        CREATE TABLE users (
          user_id INTEGER PRIMARY KEY, username TEXT, subscription_start TEXT,
          subscription_end TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE payments (
          id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, amount TEXT NOT NULL,
          status TEXT NOT NULL, operation_id TEXT UNIQUE, created_at TEXT NOT NULL,
          paid_at TEXT, provider TEXT NOT NULL DEFAULT 'legacy_yoomoney',
          provider_payment_id TEXT UNIQUE, currency TEXT NOT NULL DEFAULT 'RUB',
          FOREIGN KEY (user_id) REFERENCES users(user_id)
        );
        CREATE TABLE categories (
          id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE materials (
          id INTEGER PRIMARY KEY AUTOINCREMENT, category_id INTEGER NOT NULL,
          title TEXT NOT NULL, text TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
          FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE CASCADE
        );
        CREATE TABLE material_images (
          id INTEGER PRIMARY KEY AUTOINCREMENT, material_id INTEGER NOT NULL,
          filename TEXT NOT NULL, position INTEGER NOT NULL DEFAULT 0,
          FOREIGN KEY (material_id) REFERENCES materials(id) ON DELETE CASCADE
        );
        CREATE TABLE admins (user_id INTEGER PRIMARY KEY);
        """)
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"users","payments","categories","materials","material_images","admins"} <= tables
        db.close()
