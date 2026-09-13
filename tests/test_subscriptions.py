import asyncio
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage

from app.database import Database
from app.reminders import send_due_reminders
from test_library_api import project


def set_end(settings, end, start=None):
    with sqlite3.connect(settings.database_path) as connection:
        connection.execute("UPDATE users SET subscription_end=?, subscription_start=? WHERE user_id=2",
                           (end.isoformat() if end else None, start.isoformat() if start else None))


async def pay(db, order, days=30):
    return await db.update_yookassa_status(f"provider-{order}", "succeeded", Decimal("120.00"), "RUB",
                                          {"order_id": order, "user_id": "2"}, days)


def test_renewal_keeps_unused_days_and_duplicate_payment_is_idempotent(project):
    _, db, settings = project
    start = datetime.now(UTC) - timedelta(days=25)
    end = start + timedelta(days=30)
    set_end(settings, end, start)

    async def scenario():
        await db.reserve_yookassa_order("renew", 2, Decimal("120.00"))
        results = await asyncio.gather(pay(db, "renew"), pay(db, "renew"))
        assert sum(result.activated for result in results) == 1
        assert datetime.fromisoformat(await db.subscription_end(2)) == end + timedelta(days=30)
    asyncio.run(scenario())
    with sqlite3.connect(settings.database_path) as connection:
        assert connection.execute("SELECT subscription_start FROM users WHERE user_id=2").fetchone()[0] == start.isoformat()


def test_two_concurrent_purchases_both_extend_access(project):
    _, db, settings = project
    end = datetime.now(UTC) + timedelta(days=5)
    set_end(settings, end)

    async def scenario():
        for order in ("first", "second"):
            await db.reserve_yookassa_order(order, 2, Decimal("120.00"))
        results = await asyncio.gather(pay(db, "first"), pay(db, "second"))
        assert all(result.activated for result in results)
        assert datetime.fromisoformat(await db.subscription_end(2)) == end + timedelta(days=60)
    asyncio.run(scenario())


def test_expired_subscription_starts_from_payment_time(project):
    _, db, settings = project
    set_end(settings, datetime.now(UTC) - timedelta(days=5))
    before = datetime.now(UTC)
    asyncio.run(db.reserve_yookassa_order("restart", 2, Decimal("120.00")))
    result = asyncio.run(pay(db, "restart"))
    after = datetime.now(UTC)
    actual = datetime.fromisoformat(result.subscription_end)
    assert before + timedelta(days=30) <= actual <= after + timedelta(days=30)


def test_legacy_renewal_also_keeps_remaining_days(project):
    _, db, settings = project
    end = datetime.now(UTC) + timedelta(days=5)
    set_end(settings, end)
    asyncio.run(db.create_legacy_payment("old", 2, Decimal("120.00")))
    result = asyncio.run(db.activate_legacy_payment("old", "operation", 30))
    assert datetime.fromisoformat(result.subscription_end) == end + timedelta(days=30)
    assert not asyncio.run(db.activate_legacy_payment("old", "operation", 30)).activated


def test_daily_reminders_survive_restart_and_stop_at_expiry(project):
    _, db, settings = project
    now = datetime(2030, 1, 1, 7, tzinfo=UTC)  # 10:00 Moscow
    set_end(settings, now + timedelta(days=3))
    bot = SimpleNamespace(send_message=AsyncMock())
    asyncio.run(send_due_reminders(db, bot, settings, now=now - timedelta(seconds=1)))
    assert bot.send_message.await_count == 0
    for day in range(4):
        date = now + timedelta(days=day)
        restarted = Database(settings.database_path)
        asyncio.run(restarted.initialize())
        asyncio.run(send_due_reminders(restarted, bot, settings, now=date))
        asyncio.run(send_due_reminders(restarted, bot, settings, now=date + timedelta(minutes=1)))
        assert bot.send_message.await_count == min(day + 1, 3)
    texts = [call.args[1] for call in bot.send_message.call_args_list]
    assert "3 дней" in texts[0] and "2 дней" in texts[1] and "суток" in texts[2]
    assert all(call.args[0] == 2 for call in bot.send_message.call_args_list)
    buttons = [button for row in bot.send_message.call_args.kwargs["reply_markup"].inline_keyboard for button in row]
    assert [button.callback_data for button in buttons] == ["buy"]


def test_renewal_cancels_old_reminders_and_later_starts_new_cycle(project):
    _, db, settings = project
    now = datetime(2030, 1, 1, 7, tzinfo=UTC)
    end = now + timedelta(days=3)
    set_end(settings, end)
    bot = SimpleNamespace(send_message=AsyncMock())
    asyncio.run(send_due_reminders(db, bot, settings, now=now))
    asyncio.run(db.reserve_yookassa_order("renew", 2, Decimal("120.00")))
    asyncio.run(pay(db, "renew"))
    for day in (0, 1, 2, 3, 29):
        asyncio.run(send_due_reminders(db, bot, settings, now=now + timedelta(days=day)))
    assert bot.send_message.await_count == 1
    asyncio.run(send_due_reminders(db, bot, settings, now=now + timedelta(days=30)))
    assert bot.send_message.await_count == 2


def test_renewal_between_candidate_selection_and_send_prevents_stale_reminder(project, monkeypatch):
    _, db, settings = project
    now = datetime(2030, 1, 1, 7, tzinfo=UTC)
    end = now + timedelta(days=2)
    set_end(settings, end)
    claim = db.claim_reminder

    async def renew_after_claim(*args):
        token = await claim(*args)
        set_end(settings, end + timedelta(days=30))
        return token

    monkeypatch.setattr(db, "claim_reminder", renew_after_claim)
    bot = SimpleNamespace(send_message=AsyncMock())
    asyncio.run(send_due_reminders(db, bot, settings, now=now))
    assert bot.send_message.await_count == 0


def test_duplicate_workers_claim_once_and_failed_delivery_can_retry(project):
    _, db, settings = project
    now = datetime(2030, 1, 1, 7, tzinfo=UTC)
    set_end(settings, now + timedelta(days=2))
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=OSError("offline")))

    async def simultaneous():
        await asyncio.gather(send_due_reminders(db, bot, settings, now=now), send_due_reminders(db, bot, settings, now=now))
    asyncio.run(simultaneous())
    assert bot.send_message.await_count == 1
    bot.send_message.side_effect = None
    asyncio.run(send_due_reminders(db, bot, settings, now=now + timedelta(minutes=5)))
    assert bot.send_message.await_count == 1
    asyncio.run(send_due_reminders(db, bot, settings, now=now + timedelta(minutes=11)))
    assert bot.send_message.await_count == 2
    asyncio.run(send_due_reminders(db, bot, settings, now=now + timedelta(minutes=30)))
    assert bot.send_message.await_count == 2


def test_blocked_bot_does_not_retry_all_day(project):
    _, db, settings = project
    now = datetime(2030, 1, 1, 7, tzinfo=UTC)
    set_end(settings, now + timedelta(days=2))
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=TelegramForbiddenError(method=SendMessage(chat_id=2, text=""), message="blocked")))
    asyncio.run(send_due_reminders(db, bot, settings, now=now))
    asyncio.run(send_due_reminders(db, bot, settings, now=now + timedelta(hours=1)))
    assert bot.send_message.await_count == 1


def test_configured_reminder_hour_and_late_start(project):
    _, db, settings = project
    settings = replace(settings, subscription_reminder_hour=15)
    now = datetime(2030, 1, 1, 7, tzinfo=UTC)
    set_end(settings, now + timedelta(days=2))
    bot = SimpleNamespace(send_message=AsyncMock())
    asyncio.run(send_due_reminders(db, bot, settings, now=now))
    assert bot.send_message.await_count == 0
    asyncio.run(send_due_reminders(db, bot, settings, now=now + timedelta(hours=7)))
    assert bot.send_message.await_count == 1
