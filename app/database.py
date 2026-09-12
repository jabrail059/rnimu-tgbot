from __future__ import annotations

import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import aiosqlite


@dataclass(frozen=True)
class PaymentActivation:
    user_id: int
    activated: bool
    subscription_end: str | None


class Database:
    def __init__(self, path: str):
        self.path = path

    @asynccontextmanager
    async def _connect(self):
        async with aiosqlite.connect(self.path, timeout=10) as connection:
            await connection.execute("PRAGMA foreign_keys = ON")
            yield connection

    async def initialize(self) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        async with self._connect() as db:
            await db.execute("PRAGMA journal_mode = WAL")
            await db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
              user_id INTEGER PRIMARY KEY, username TEXT, subscription_start TEXT,
              subscription_end TEXT, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS payments (
              id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, amount TEXT NOT NULL,
              status TEXT NOT NULL, operation_id TEXT UNIQUE, created_at TEXT NOT NULL,
              paid_at TEXT, provider TEXT NOT NULL DEFAULT 'legacy_yoomoney',
              provider_payment_id TEXT UNIQUE, currency TEXT NOT NULL DEFAULT 'RUB',
              FOREIGN KEY (user_id) REFERENCES users(user_id)
            );
            CREATE TABLE IF NOT EXISTS schema_migrations (
              version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
            );
            """)
            # The pre-YooKassa implementation used a non-standard final status.
            await db.execute("UPDATE payments SET status='succeeded' WHERE status='success'")
            columns = {row[1] for row in await (await db.execute("PRAGMA table_info(users)")).fetchall()}
            if "subscription_start" not in columns:
                await db.execute("ALTER TABLE users ADD COLUMN subscription_start TEXT")
            payment_columns = {row[1] for row in await (await db.execute("PRAGMA table_info(payments)")).fetchall()}
            for name, definition in (("provider", "TEXT NOT NULL DEFAULT 'legacy_yoomoney'"), ("provider_payment_id", "TEXT"), ("currency", "TEXT NOT NULL DEFAULT 'RUB'")):
                if name not in payment_columns:
                    await db.execute(f"ALTER TABLE payments ADD COLUMN {name} {definition}")
            await db.executescript("""
              CREATE INDEX IF NOT EXISTS idx_payments_user_created ON payments(user_id, created_at DESC);
              CREATE INDEX IF NOT EXISTS idx_payments_provider_payment ON payments(provider, provider_payment_id);
              CREATE INDEX IF NOT EXISTS idx_users_subscription_end ON users(subscription_end);
            """)
            await db.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (1, ?)", (datetime.now(UTC).isoformat(),))
            await db.executescript("""
              CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL, created_at TEXT NOT NULL
              );
              CREATE TABLE IF NOT EXISTS materials (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
                title TEXT NOT NULL, text TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
              );
              CREATE TABLE IF NOT EXISTS material_images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                material_id INTEGER NOT NULL REFERENCES materials(id) ON DELETE CASCADE,
                filename TEXT NOT NULL UNIQUE, position INTEGER NOT NULL DEFAULT 0
              );
              CREATE INDEX IF NOT EXISTS idx_materials_category ON materials(category_id, id);
              CREATE INDEX IF NOT EXISTS idx_images_material ON material_images(material_id, position, id);
            """)
            await db.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (2, ?)", (datetime.now(UTC).isoformat(),))
            await db.commit()

    async def categories(self) -> list[dict]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute("""SELECT c.id, c.title, COUNT(m.id) AS material_count
                FROM categories c LEFT JOIN materials m ON m.category_id=c.id
                GROUP BY c.id ORDER BY c.id""")).fetchall()
            return [dict(row) for row in rows]

    async def category(self, category_id: int) -> dict | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute("SELECT id, title FROM categories WHERE id=?", (category_id,))).fetchone()
            return dict(row) if row else None

    async def save_category(self, title: str, category_id: int | None = None) -> int | None:
        async with self._connect() as db:
            if category_id is None:
                cursor = await db.execute("INSERT INTO categories(title, created_at) VALUES (?, ?)", (title, datetime.now(UTC).isoformat()))
                category_id = cursor.lastrowid
            else:
                cursor = await db.execute("UPDATE categories SET title=? WHERE id=?", (title, category_id))
                if not cursor.rowcount:
                    return None
            await db.commit()
            return category_id

    async def materials(self, category_id: int) -> list[dict]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute("""SELECT m.id, m.title, COUNT(i.id) AS image_count
                FROM materials m LEFT JOIN material_images i ON i.material_id=m.id
                WHERE m.category_id=? GROUP BY m.id ORDER BY m.id""", (category_id,))).fetchall()
            return [dict(row) for row in rows]

    async def material(self, material_id: int) -> dict | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute("SELECT id, category_id, title, text FROM materials WHERE id=?", (material_id,))).fetchone()
            if not row:
                return None
            images = await (await db.execute("SELECT id, position FROM material_images WHERE material_id=? ORDER BY position, id", (material_id,))).fetchall()
            return {**dict(row), "images": [dict(image) for image in images]}

    async def save_material(self, category_id: int, title: str, text: str, material_id: int | None = None) -> int | None:
        async with self._connect() as db:
            if material_id is None:
                cursor = await db.execute("INSERT INTO materials(category_id, title, text, created_at) VALUES (?, ?, ?, ?)", (category_id, title, text, datetime.now(UTC).isoformat()))
                material_id = cursor.lastrowid
            else:
                cursor = await db.execute("UPDATE materials SET category_id=?, title=?, text=? WHERE id=?", (category_id, title, text, material_id))
                if not cursor.rowcount:
                    return None
            await db.commit()
            return material_id

    async def add_image(self, material_id: int, filename: str) -> int:
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute("""INSERT INTO material_images(material_id, filename, position)
                SELECT ?, ?, COALESCE(MAX(position), -1) + 1 FROM material_images WHERE material_id=?""", (material_id, filename, material_id))
            await db.commit()
            return cursor.lastrowid

    async def image(self, image_id: int) -> dict | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute("SELECT * FROM material_images WHERE id=?", (image_id,))).fetchone()
            return dict(row) if row else None

    async def delete_content(self, kind: str, content_id: int) -> list[str] | None:
        # Fixed table/column allowlist; no caller-controlled SQL identifiers.
        queries = {
            "category": ("categories", "SELECT filename FROM material_images WHERE material_id IN (SELECT id FROM materials WHERE category_id=?)"),
            "material": ("materials", "SELECT filename FROM material_images WHERE material_id=?"),
            "image": ("material_images", "SELECT filename FROM material_images WHERE id=?"),
        }
        table, images_query = queries[kind]
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            images = await (await db.execute(images_query, (content_id,))).fetchall()
            cursor = await db.execute(f"DELETE FROM {table} WHERE id=?", (content_id,))
            await db.commit()
            return [row[0] for row in images] if cursor.rowcount else None

    async def upsert_user(self, user_id: int, username: str | None) -> None:
        async with self._connect() as db:
            await db.execute("""INSERT INTO users(user_id, username, created_at) VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET username=excluded.username""", (user_id, username, datetime.now(UTC).isoformat()))
            await db.commit()

    async def reserve_yookassa_order(self, order_id: str, user_id: int, amount: Decimal) -> None:
        async with self._connect() as db:
            await db.execute("""INSERT INTO payments(id, provider, user_id, amount, currency, status, created_at)
                VALUES (?, 'yookassa', ?, ?, 'RUB', 'pending', ?)""", (order_id, user_id, f"{amount:.2f}", datetime.now(UTC).isoformat()))
            await db.commit()

    async def attach_yookassa_payment(self, order_id: str, provider_payment_id: str) -> None:
        async with self._connect() as db:
            cursor = await db.execute("""UPDATE payments SET provider_payment_id=?
                WHERE id=? AND provider='yookassa' AND provider_payment_id IS NULL""", (provider_payment_id, order_id))
            if cursor.rowcount != 1:
                raise RuntimeError("Cannot attach YooKassa payment to order")
            await db.commit()

    async def create_legacy_payment(self, payment_id: str, user_id: int, amount: Decimal) -> None:
        async with self._connect() as db:
            await db.execute("""INSERT INTO payments(id, provider, user_id, amount, currency, status, created_at)
                VALUES (?, 'legacy_yoomoney', ?, ?, 'RUB', 'pending', ?)""", (payment_id, user_id, f"{amount:.2f}", datetime.now(UTC).isoformat()))
            await db.commit()

    async def update_yookassa_status(self, provider_payment_id: str, status: str, amount: Decimal, currency: str, metadata: dict[str, str], days: int) -> PaymentActivation | None:
        """Verify order data and atomically activate only an unprocessed succeeded payment."""
        now = datetime.now(UTC)
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            order_id_from_metadata = metadata.get("order_id", "")
            row = await (await db.execute("""SELECT id, user_id, amount, currency, status, provider_payment_id FROM payments
                WHERE provider='yookassa' AND id=?""", (order_id_from_metadata,))).fetchone()
            if not row:
                await db.rollback(); return None
            order_id, user_id, expected_amount, expected_currency, old_status, stored_provider_id = row
            if metadata.get("user_id") != str(user_id) or Decimal(expected_amount) != amount or expected_currency != currency or (stored_provider_id and stored_provider_id != provider_payment_id):
                await db.rollback(); return None
            if stored_provider_id is None:
                await db.execute("UPDATE payments SET provider_payment_id=? WHERE id=?", (provider_payment_id, order_id))
            if status not in {"pending", "waiting_for_capture", "succeeded", "canceled"}:
                await db.rollback(); return None
            if old_status == "succeeded":
                existing = await (await db.execute("SELECT subscription_end FROM users WHERE user_id=?", (user_id,))).fetchone()
                await db.commit(); return PaymentActivation(user_id, False, existing[0] if existing else None)
            await db.execute("UPDATE payments SET status=?, paid_at=? WHERE id=?", (status, now.isoformat() if status == "succeeded" else None, order_id))
            if status != "succeeded":
                await db.commit(); return PaymentActivation(user_id, False, None)
            existing = await (await db.execute("SELECT subscription_end FROM users WHERE user_id=?", (user_id,))).fetchone()
            previous_end = self._parse_datetime(existing[0]) if existing and existing[0] else None
            base = previous_end if previous_end and previous_end > now else now
            subscription_end = (base + timedelta(days=days)).isoformat()
            if not previous_end or previous_end <= now:
                await db.execute("UPDATE users SET subscription_start=?, subscription_end=? WHERE user_id=?", (now.isoformat(), subscription_end, user_id))
            else:
                await db.execute("UPDATE users SET subscription_end=? WHERE user_id=?", (subscription_end, user_id))
            await db.commit(); return PaymentActivation(user_id, True, subscription_end)

    async def activate_legacy_payment(self, payment_id: str, operation_id: str, days: int) -> PaymentActivation | None:
        """Temporary compatibility path; remove after Quickpay migration is accepted."""
        now = datetime.now(UTC)
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (await db.execute("SELECT user_id, status FROM payments WHERE id=? AND provider='legacy_yoomoney'", (payment_id,))).fetchone()
            if not row:
                await db.rollback(); return None
            user_id, status = row
            if status == "succeeded":
                await db.commit(); return PaymentActivation(user_id, False, None)
            existing = await (await db.execute("SELECT subscription_end FROM users WHERE user_id=?", (user_id,))).fetchone()
            previous_end = self._parse_datetime(existing[0]) if existing and existing[0] else None
            base = previous_end if previous_end and previous_end > now else now
            subscription_end = (base + timedelta(days=days)).isoformat()
            await db.execute("UPDATE payments SET status='succeeded', operation_id=?, paid_at=? WHERE id=?", (operation_id, now.isoformat(), payment_id))
            await db.execute("UPDATE users SET subscription_start=COALESCE(subscription_start, ?), subscription_end=? WHERE user_id=?", (now.isoformat(), subscription_end, user_id))
            await db.commit(); return PaymentActivation(user_id, True, subscription_end)

    async def subscription_end(self, user_id: int) -> str | None:
        async with self._connect() as db:
            row = await (await db.execute("SELECT subscription_end FROM users WHERE user_id=?", (user_id,))).fetchone()
        end = self._parse_datetime(row[0]) if row and row[0] else None
        return row[0] if end and end > datetime.now(UTC) else None

    @staticmethod
    def _parse_datetime(value: str) -> datetime | None:
        try:
            date = datetime.fromisoformat(value)
            return date if date.tzinfo else date.replace(tzinfo=UTC)
        except (TypeError, ValueError):
            return None
