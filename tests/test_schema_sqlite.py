from pathlib import Path
import sqlite3
import tempfile
import asyncio

from app.database import Database

# Schema snapshot of the earlier admin release, used as an upgrade fixture.
def test_existing_admin_schema_survives_upgrade():
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
        db.execute("INSERT INTO users(user_id, username, subscription_end, created_at) VALUES (7, 'existing', '2099-01-01T00:00:00+00:00', '2026-09-09')")
        db.execute("INSERT INTO categories(id, title, created_at) VALUES (3, 'Existing category', '2026-09-09')")
        db.execute("INSERT INTO materials(id, category_id, title, text, created_at) VALUES (4, 3, 'Existing material', 'Saved text', '2026-09-09')")
        db.execute("INSERT INTO material_images(id, material_id, filename, position) VALUES (5, 4, ?, 0)", ("a" * 32 + ".png",))
        db.execute("INSERT INTO admins(user_id) VALUES (7)")
        db.commit()
        db.close()
        current = Database(str(Path(tmp) / "app.db"))
        asyncio.run(current.initialize())
        asyncio.run(current.initialize())
        material = asyncio.run(current.material(4))
        assert material["text"] == "Saved text"
        assert material["images"] == [{"id": 5, "position": 0}]
        assert asyncio.run(current.image(5))["filename"] == "a" * 32 + ".png"
        assert asyncio.run(current.subscription_end(7)) == "2099-01-01T00:00:00+00:00"
        assert asyncio.run(current.delete_content("category", 3)) == ["a" * 32 + ".png"]
        assert asyncio.run(current.material(4)) is None
        assert asyncio.run(current.image(5)) is None
