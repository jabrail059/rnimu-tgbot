"""Persistent session, replay and content controls shared by API workers."""
from __future__ import annotations

import math
import secrets
import time
from dataclasses import dataclass

import aiosqlite
from fastapi import HTTPException

from app.proofs import Proof, digest

SCHEMA = """
CREATE TABLE IF NOT EXISTS reader_sessions (
  user_id INTEGER PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, key_id TEXT NOT NULL,
  expires REAL NOT NULL, idle_expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS reader_launches (
  launch_hash TEXT PRIMARY KEY, key_id TEXT NOT NULL, expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS reader_proofs (
  key_id TEXT NOT NULL, jti TEXT NOT NULL, expires REAL NOT NULL, PRIMARY KEY(key_id, jti)
);
CREATE INDEX IF NOT EXISTS idx_reader_proofs_expiry ON reader_proofs(expires);
CREATE TABLE IF NOT EXISTS reader_views (
  user_id INTEGER PRIMARY KEY, session_hash TEXT NOT NULL, view_hash TEXT UNIQUE NOT NULL,
  material_id INTEGER NOT NULL REFERENCES materials(id) ON DELETE CASCADE, expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS reader_tickets (
  ticket_hash TEXT PRIMARY KEY, session_hash TEXT NOT NULL, view_hash TEXT NOT NULL,
  resource TEXT NOT NULL, expires REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reader_tickets_expiry ON reader_tickets(expires);
CREATE TABLE IF NOT EXISTS reader_events (
  user_id INTEGER NOT NULL, resource TEXT NOT NULL, at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reader_events_user_time ON reader_events(user_id, at);
CREATE TABLE IF NOT EXISTS reader_requests (user_id INTEGER NOT NULL, at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_reader_requests_user_time ON reader_requests(user_id, at);
CREATE TABLE IF NOT EXISTS reader_blocks (user_id INTEGER PRIMARY KEY, until REAL NOT NULL);
CREATE TABLE IF NOT EXISTS reader_audit (user_id INTEGER NOT NULL, event TEXT NOT NULL, at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_reader_audit_time ON reader_audit(at);
"""


@dataclass(frozen=True)
class ReaderSession:
    user_id: int
    token_hash: str
    expires: float


class AccessControl:
    def __init__(self, database, settings):
        self.database = database
        self.settings = settings

    async def _proof(self, db, proof: Proof, now: float):
        await db.execute("DELETE FROM reader_proofs WHERE expires<=?", (now,))
        try:
            await db.execute("INSERT INTO reader_proofs VALUES (?, ?, ?)", (proof.key_id, proof.jti, now + 120))
        except aiosqlite.IntegrityError as exc:
            raise HTTPException(401, "Повторный запрос отклонён. Повторите действие в приложении.") from exc

    async def _audit(self, db, user_id: int, event: str, now: float):
        await db.execute("INSERT INTO reader_audit VALUES (?, ?, ?)", (user_id, event, now))
        await db.execute("DELETE FROM reader_audit WHERE at<?", (now - 30 * 86400,))

    async def create_session(self, user_id: int, launch_hash: str, proof: Proof) -> tuple[str, float]:
        now = time.time()
        token = secrets.token_urlsafe(32)
        expires = now + 4 * 3600
        async with self.database._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            await self._proof(db, proof, now)
            await db.execute("DELETE FROM reader_launches WHERE expires<=?", (now,))
            launch = await (await db.execute("SELECT key_id FROM reader_launches WHERE launch_hash=?", (launch_hash,))).fetchone()
            if launch and launch[0] != proof.key_id:
                await self._audit(db, user_id, "launch_replay", now)
                await db.commit()
                raise HTTPException(401, "Эти данные входа уже использованы на другом устройстве. Откройте приложение заново из бота.")
            recent = await (await db.execute("SELECT COUNT(*) FROM reader_audit WHERE user_id=? AND event='session_opened' AND at>?", (user_id, now - 60))).fetchone()
            if recent[0] >= 6:
                await db.commit()
                raise HTTPException(429, "Слишком много входов. Подождите минуту.", headers={"Retry-After": "60"})
            await db.execute("INSERT OR IGNORE INTO reader_launches VALUES (?, ?, ?)", (launch_hash, proof.key_id, now + 3600))
            # One active session per account. An old client cannot renew itself
            # using a stale token; opening a new session requires Telegram again.
            await db.execute("""INSERT INTO reader_sessions VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET token_hash=excluded.token_hash, key_id=excluded.key_id,
                expires=excluded.expires, idle_expires=excluded.idle_expires""",
                (user_id, digest(token), proof.key_id, expires, now + 900))
            await db.execute("DELETE FROM reader_views WHERE user_id=?", (user_id,))
            await db.execute("DELETE FROM reader_sessions WHERE expires<=?", (now,))
            await self._audit(db, user_id, "session_opened", now)
            await db.commit()
        return token, expires

    async def authenticate(self, token: str, proof: Proof) -> ReaderSession:
        now = time.time()
        token_hash = digest(token)
        async with self.database._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (await db.execute("SELECT user_id, key_id, expires, idle_expires FROM reader_sessions WHERE token_hash=?", (token_hash,))).fetchone()
            if not row or row[1] != proof.key_id or min(row[2], row[3]) <= now:
                raise HTTPException(401, "Сеанс завершён или приложение открыто в другом окне. Откройте его заново из бота.")
            await self._proof(db, proof, now)
            await db.execute("DELETE FROM reader_requests WHERE at<=?", (now - 60,))
            count = await (await db.execute("SELECT COUNT(*) FROM reader_requests WHERE user_id=?", (row[0],))).fetchone()
            if count[0] >= 240:
                await db.commit()
                raise HTTPException(429, "Слишком много действий. Подождите минуту.", headers={"Retry-After": "60"})
            await db.execute("INSERT INTO reader_requests VALUES (?, ?)", (row[0], now))
            await db.execute("UPDATE reader_sessions SET idle_expires=? WHERE token_hash=?", (min(row[2], now + 900), token_hash))
            await db.commit()
        return ReaderSession(row[0], token_hash, row[2])

    async def _current(self, db, session: ReaderSession, now: float):
        row = await (await db.execute("SELECT 1 FROM reader_sessions WHERE user_id=? AND token_hash=? AND expires>? AND idle_expires>?",
                                     (session.user_id, session.token_hash, now, now))).fetchone()
        if not row:
            raise HTTPException(401, "Сеанс завершён. Откройте приложение заново из бота.")
        if session.user_id not in self.settings.admin_ids:
            row = await (await db.execute("SELECT subscription_end FROM users WHERE user_id=?", (session.user_id,))).fetchone()
            expiry = self.database._parse_datetime(row[0]) if row and row[0] else None
            if not expiry or expiry.timestamp() <= now:
                raise HTTPException(403, "Нужна активная подписка.")

    async def _view(self, db, session: ReaderSession, view: str, now: float):
        await self._current(db, session, now)
        row = await (await db.execute("SELECT material_id FROM reader_views WHERE user_id=? AND session_hash=? AND view_hash=? AND expires>?",
                                     (session.user_id, session.token_hash, digest(view), now))).fetchone()
        if not row:
            raise HTTPException(409, "Просмотр завершён. Откройте материал заново.")
        return row[0]

    def _limited(self, seconds: float):
        delay = max(1, math.ceil(seconds))
        return HTTPException(429, f"Достигнут лимит чтения. Повторите через {math.ceil(delay / 60)} мин. или обратитесь к автору курса.",
                             headers={"Retry-After": str(delay), "X-Reading-Limited": "1"})

    async def _allow(self, db, session: ReaderSession, resource: str, now: float):
        if session.user_id in self.settings.admin_ids:
            return
        block = await (await db.execute("SELECT until FROM reader_blocks WHERE user_id=?", (session.user_id,))).fetchone()
        if block and block[0] > now:
            raise self._limited(block[0] - now)
        await db.execute("DELETE FROM reader_events WHERE at<=?", (now - 86400,))
        limits = ((60, self.settings.read_pages_per_minute), (3600, self.settings.read_pages_per_hour), (86400, self.settings.read_pages_per_day))
        for window, limit in limits:
            already = await (await db.execute("SELECT 1 FROM reader_events WHERE user_id=? AND resource=? AND at>? LIMIT 1",
                                             (session.user_id, resource, now - window))).fetchone()
            if already:
                continue
            count = await (await db.execute("SELECT COUNT(DISTINCT resource), MIN(at) FROM reader_events WHERE user_id=? AND at>?",
                                           (session.user_id, now - window))).fetchone()
            if count[0] >= limit:
                delay = min(600, max(1, count[1] + window - now))
                await db.execute("INSERT INTO reader_blocks VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET until=excluded.until", (session.user_id, now + delay))
                await self._audit(db, session.user_id, f"reading_limit_{window}", now)
                await db.commit()
                raise self._limited(delay)
        # Bound repeat-event growth; already read pages remain free of distinct quotas.
        recent = await (await db.execute("SELECT 1 FROM reader_events WHERE user_id=? AND resource=? AND at>? LIMIT 1", (session.user_id, resource, now - 30))).fetchone()
        if not recent:
            await db.execute("INSERT INTO reader_events VALUES (?, ?, ?)", (session.user_id, resource, now))

    async def open_view(self, session: ReaderSession, material_id: int) -> str:
        now = time.time()
        view = secrets.token_urlsafe(32)
        async with self.database._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            await self._current(db, session, now)
            await self._allow(db, session, f"material:{material_id}", now)
            await db.execute("""INSERT INTO reader_views VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET session_hash=excluded.session_hash,
                view_hash=excluded.view_hash, material_id=excluded.material_id, expires=excluded.expires""",
                (session.user_id, session.token_hash, digest(view), material_id, now + 90))
            await db.commit()
        return view

    async def heartbeat(self, session: ReaderSession, view: str) -> None:
        now = time.time()
        async with self.database._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            await self._view(db, session, view, now)
            block = await (await db.execute("SELECT until FROM reader_blocks WHERE user_id=?", (session.user_id,))).fetchone()
            if block and block[0] > now and session.user_id not in self.settings.admin_ids:
                raise self._limited(block[0] - now)
            await db.execute("UPDATE reader_views SET expires=? WHERE view_hash=?", (now + 90, digest(view)))
            await db.commit()

    async def close_view(self, session: ReaderSession, view: str) -> None:
        async with self.database._connect() as db:
            await db.execute("DELETE FROM reader_views WHERE user_id=? AND session_hash=? AND view_hash=?", (session.user_id, session.token_hash, digest(view)))
            await db.commit()

    async def issue_ticket(self, session: ReaderSession, view: str, kind: str, resource_id: int, page: int) -> str:
        now = time.time()
        ticket = secrets.token_urlsafe(32)
        async with self.database._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            material_id = await self._view(db, session, view, now)
            if kind == "image":
                row = await (await db.execute("SELECT material_id FROM material_images WHERE id=?", (resource_id,))).fetchone()
                valid = row and row[0] == material_id and page == 1
            else:
                row = await (await db.execute("SELECT material_id, json_array_length(page_sizes) FROM material_documents WHERE id=?", (resource_id,))).fetchone()
                valid = row and row[0] == material_id and 1 <= page <= row[1]
            if not valid:
                raise HTTPException(404, "Страница не найдена в открытом материале.")
            resource = f"{kind}:{resource_id}:{page}"
            # Count at issuance as well as consumption: pre-minting many tickets
            # cannot reserve the whole course before a limit is checked.
            await self._allow(db, session, resource, now)
            await db.execute("DELETE FROM reader_tickets WHERE expires<=?", (now,))
            pending = await (await db.execute("SELECT COUNT(*) FROM reader_tickets WHERE session_hash=?", (session.token_hash,))).fetchone()
            if pending[0] >= 8:
                raise HTTPException(429, "Дождитесь загрузки текущих страниц.", headers={"Retry-After": "30"})
            await db.execute("INSERT INTO reader_tickets VALUES (?, ?, ?, ?, ?)", (digest(ticket), session.token_hash, digest(view), resource, now + 30))
            await db.commit()
        return ticket

    async def consume_ticket(self, session: ReaderSession, view: str, ticket: str, resource: str):
        now = time.time()
        if not ticket or len(ticket) > 128:
            raise HTTPException(403, "Откройте страницу через просмотр материала.")
        async with self.database._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            await self._view(db, session, view, now)
            cursor = await db.execute("DELETE FROM reader_tickets WHERE ticket_hash=? AND session_hash=? AND view_hash=? AND resource=? AND expires>?",
                                      (digest(ticket), session.token_hash, digest(view), resource, now))
            if not cursor.rowcount:
                raise HTTPException(403, "Разрешение на страницу истекло или уже использовано. Повторите загрузку.")
            await self._allow(db, session, resource, now)
            await db.commit()

    async def check_delivery(self, session: ReaderSession, view: str):
        async with self.database._connect() as db:
            await self._view(db, session, view, time.time())

    async def audit(self) -> list[dict]:
        async with self.database._connect() as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute("SELECT user_id, event, at FROM reader_audit ORDER BY at DESC LIMIT 100")).fetchall()
            return [dict(row) for row in rows]

    async def manage_user(self, user_id: int, action: str):
        async with self.database._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            if action == "revoke":
                await db.execute("DELETE FROM reader_sessions WHERE user_id=?", (user_id,))
                await db.execute("DELETE FROM reader_views WHERE user_id=?", (user_id,))
            else:
                await db.execute("DELETE FROM reader_blocks WHERE user_id=?", (user_id,))
                await db.execute("DELETE FROM reader_events WHERE user_id=?", (user_id,))
                await db.execute("DELETE FROM reader_requests WHERE user_id=?", (user_id,))
            await self._audit(db, user_id, f"admin_{action}", time.time())
            await db.commit()
