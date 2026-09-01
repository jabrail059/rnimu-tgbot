from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import aiosqlite


class Database:
    def __init__(self, path: str):
        self.path = path

    async def initialize(self) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
              user_id INTEGER PRIMARY KEY,
              username TEXT,
              subscription_end TEXT,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS payments (
              id TEXT PRIMARY KEY,
              user_id INTEGER NOT NULL,
              amount TEXT NOT NULL,
              status TEXT NOT NULL,
              operation_id TEXT UNIQUE,
              created_at TEXT NOT NULL,
              paid_at TEXT,
              FOREIGN KEY (user_id) REFERENCES users(user_id)
            );
            """)
            await db.commit()

    async def upsert_user(self, user_id: int, username: str | None) -> None:
        now = datetime.now(UTC).isoformat()
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""
                INSERT INTO users(user_id, username, created_at) VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET username=excluded.username
            """, (user_id, username, now))
            await db.commit()

    async def create_payment(self, payment_id: str, user_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO payments(id, user_id, amount, status, created_at) VALUES (?, ?, '120.00', 'pending', ?)",
                (payment_id, user_id, datetime.now(UTC).isoformat()),
            )
            await db.commit()

    async def activate_payment(self, payment_id: str, operation_id: str) -> int | None:
        """Idempotently mark a payment successful and extend its user's subscription."""
        now = datetime.now(UTC)
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (await db.execute("SELECT user_id, status FROM payments WHERE id=?", (payment_id,))).fetchone()
            if not row:
                await db.rollback()
                return None
            user_id, status = row
            if status == "success":
                await db.commit()
                return user_id
            existing = await (await db.execute("SELECT subscription_end FROM users WHERE user_id=?", (user_id,))).fetchone()
            base = now
            if existing and existing[0]:
                try:
                    previous = datetime.fromisoformat(existing[0])
                    if previous > now:
                        base = previous
                except ValueError:
                    pass
            subscription_end = (base + timedelta(days=30)).isoformat()
            await db.execute("UPDATE payments SET status='success', operation_id=?, paid_at=? WHERE id=?", (operation_id, now.isoformat(), payment_id))
            await db.execute("UPDATE users SET subscription_end=? WHERE user_id=?", (subscription_end, user_id))
            await db.commit()
            return user_id

    async def subscription_end(self, user_id: int) -> str | None:
        async with aiosqlite.connect(self.path) as db:
            row = await (await db.execute("SELECT subscription_end FROM users WHERE user_id=?", (user_id,))).fetchone()
        if not row or not row[0]:
            return None
        try:
            return row[0] if datetime.fromisoformat(row[0]) > datetime.now(UTC) else None
        except ValueError:
            return None
