from __future__ import annotations

import asyncio
import logging
import math
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import Settings
from app.database import Database

logger = logging.getLogger(__name__)
MOSCOW = ZoneInfo("Europe/Moscow")


async def send_due_reminders(database: Database, bot, settings: Settings, *, now: datetime | None = None) -> None:
    current = now or datetime.now(UTC)
    local = current.astimezone(MOSCOW)
    if local.hour < settings.subscription_reminder_hour:
        return
    for user_id, end in await database.reminder_candidates(current):
        # A large recipient list can take minutes to send. Date and lease time
        # must describe this recipient's attempt, not the start of the batch.
        current = now or datetime.now(UTC)
        local = current.astimezone(MOSCOW)
        if local.hour < settings.subscription_reminder_hour:
            return
        token = await database.claim_reminder(user_id, end, local.date().isoformat(), current)
        if not token:
            continue
        # Recheck after claiming, in case a payment extended the subscription.
        sending_at = now or datetime.now(UTC)
        if not await database.reminder_is_current(user_id, end, sending_at):
            continue
        expiry = database._parse_datetime(end)
        days = math.ceil((expiry - sending_at).total_seconds() / 86400)
        remaining = "не больше суток" if days == 1 else f"не больше {days} дней"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Продлить подписку", callback_data="buy")],
            [InlineKeyboardButton(text="Главное меню", callback_data="menu")],
        ])
        try:
            await bot.send_message(user_id,
                f"До окончания подписки осталось {remaining}.\n"
                f"Доступ действует до {expiry.astimezone(MOSCOW):%d.%m.%Y %H:%M} (МСК).\n\n"
                "При продлении оставшиеся дни сохранятся. Если вы уже оплатили, дождитесь подтверждения платежа.",
                reply_markup=keyboard)
        except TelegramRetryAfter:
            # Respect flood control; pending leases become retryable after 10 min.
            raise
        except (TelegramForbiddenError, TelegramBadRequest):
            logger.info("Cannot deliver subscription reminder to user %s", user_id)
            await database.finish_reminder(token, sending_at)
        except Exception:
            # No successful mark: retry after lease expiry, including after restart.
            logger.exception("Subscription reminder delivery failed for user %s", user_id)
        else:
            await database.finish_reminder(token, sending_at)
        await asyncio.sleep(0.05)


async def run_reminders(database: Database, bot, settings: Settings) -> None:
    while True:
        delay = 60
        try:
            await send_due_reminders(database, bot, settings)
        except TelegramRetryAfter as exc:
            delay = max(60, exc.retry_after + 1)
        except Exception:
            logger.exception("Subscription reminder check failed")
        await asyncio.sleep(delay)
