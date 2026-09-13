from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import uvicorn
from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonWebApp, Message, WebAppInfo
from yoomoney import Quickpay

from app.config import get_settings
from app.database import Database
from app.reminders import run_reminders
from app.web import create_app
from app.yookassa import YooKassaClient, YooKassaError

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)
settings = get_settings()
database = Database(settings.database_path)
session = AiohttpSession(proxy=settings.proxy_url) if settings.proxy_url else AiohttpSession()
bot = Bot(settings.bot_token, session=session)
dp = Dispatcher()
yookassa = YooKassaClient(settings)


def main_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"Купить / продлить — {settings.subscription_price:.2f} ₽", callback_data="buy")]]
    if settings.enable_legacy_yoomoney:
        rows.append([InlineKeyboardButton(text="Оплатить через старый YooMoney (временно)", callback_data="buy_legacy")])
    rows.append([InlineKeyboardButton(text="Открыть приложение", web_app=WebAppInfo(url=settings.public_base_url))])
    rows.append([InlineKeyboardButton(text="Главное меню", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(CommandStart())
@dp.message(Command("menu"))
async def start(message: Message) -> None:
    if not message.from_user:
        return
    await send_menu(message, message.from_user)


async def send_menu(message: Message, user) -> None:
    if message.chat.type != "private":
        await message.answer("Откройте личный чат с ботом и отправьте /menu.")
        return
    await database.upsert_user(user.id, user.username)
    subscription_end = await database.subscription_end(user.id)
    status = "Подписка неактивна"
    if subscription_end:
        end = datetime.fromisoformat(subscription_end).astimezone(ZoneInfo("Europe/Moscow"))
        status = f"Подписка активна\nДействует до: {end:%d.%m.%Y %H:%M} (МСК)"
    await message.answer(
        "Патанатомия с @eucliris\n\n"
        "Учебные материалы по разделам: тексты, фотографии препаратов и PDF. "
        "Новые материалы постепенно появляются в приложении.\n\n"
        f"{status}\n\n"
        f"Доступ ко всем материалам — {settings.subscription_price:.2f} ₽ на {settings.subscription_days} дней.",
        reply_markup=main_keyboard(),
    )


@dp.message(Command("id"))
async def show_user_id(message: Message) -> None:
    if message.from_user and message.chat.type == "private":
        await message.answer(f"Ваш Telegram ID: {message.from_user.id}")


@dp.callback_query(F.data.in_({"menu", "course_info"}))
async def menu_callback(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message:
        await send_menu(callback.message, callback.from_user)


@dp.callback_query(F.data == "buy")
async def buy(callback: CallbackQuery) -> None:
    user = callback.from_user
    await callback.answer("Создаём платёж…")
    await database.upsert_user(user.id, user.username)
    order_id = f"pathology-{uuid.uuid4().hex}"
    try:
        # Persist before the provider call: even an unusually fast webhook can be verified.
        await database.reserve_yookassa_order(order_id, user.id, settings.subscription_price)
        payment = await yookassa.create_payment(order_id, user.id, settings.subscription_price)
        if payment.status != "pending" or not payment.confirmation_url:
            raise YooKassaError("YooKassa did not return a checkout URL")
        await database.attach_yookassa_payment(order_id, payment.id)
    except YooKassaError:
        logger.exception("Cannot create YooKassa payment for Telegram user %s", user.id)
        await callback.message.answer("Не удалось создать платёж. Попробуйте ещё раз через минуту.")
        return
    except Exception:
        logger.exception("Cannot store YooKassa payment for Telegram user %s", user.id)
        await callback.message.answer("Не удалось подготовить платёж. Попробуйте ещё раз через минуту.")
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Перейти к оплате", url=payment.confirmation_url)]])
    await callback.message.answer("На странице ЮKassa будут доступны банковская карта, СБП и другие подключённые для магазина способы. Доступ откроется только после подтверждения оплаты ЮKassa.", reply_markup=keyboard)


@dp.callback_query(F.data == "buy_legacy")
async def buy_legacy(callback: CallbackQuery) -> None:
    if not settings.enable_legacy_yoomoney or not settings.yoomoney_wallet:
        await callback.answer("Старый способ оплаты отключён", show_alert=True)
        return
    user = callback.from_user
    await database.upsert_user(user.id, user.username)
    payment_id = f"pathology-{uuid.uuid4().hex}"
    await database.create_legacy_payment(payment_id, user.id, settings.subscription_price)
    quickpay = Quickpay(receiver=settings.yoomoney_wallet, quickpay_form="shop", targets="Подписка на материалы по патанатомии", paymentType="AC", sum=float(settings.subscription_price), label=payment_id)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Перейти к оплате", url=quickpay.redirected_url)]])
    await callback.message.answer("Временный legacy-поток. Доступ также выдаётся только по серверному уведомлению.", reply_markup=keyboard)
    await callback.answer()


@dp.error()
async def on_bot_error(event) -> bool:
    logger.exception("Unhandled Telegram update error: %s", event.exception)
    return True


async def run() -> None:
    server: uvicorn.Server | None = None
    server_task: asyncio.Task[None] | None = None
    polling_task: asyncio.Task[None] | None = None
    reminder_task: asyncio.Task[None] | None = None
    try:
        await database.initialize()
        await bot.set_my_commands([
            BotCommand(command="start", description="Начать знакомство с курсом"),
            BotCommand(command="menu", description="Меню и статус подписки"),
            BotCommand(command="id", description="Узнать свой Telegram ID"),
        ])
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Материалы", web_app=WebAppInfo(url=settings.public_base_url)))
        web_app = create_app(settings, database, bot)
        server = uvicorn.Server(uvicorn.Config(web_app, host="127.0.0.1", port=8000, log_level=os.getenv("LOG_LEVEL", "info").lower(), proxy_headers=True, forwarded_allow_ips="127.0.0.1"))
        server_task = asyncio.create_task(server.serve(), name="uvicorn")
        polling_task = asyncio.create_task(dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types()), name="polling")
        reminder_task = asyncio.create_task(run_reminders(database, bot, settings), name="subscription-reminders")
        done, _ = await asyncio.wait({server_task, polling_task, reminder_task}, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
        raise RuntimeError("Bot or API server stopped unexpectedly")
    finally:
        if server:
            server.should_exit = True
        for task in (polling_task, reminder_task):
            if task and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (server_task, polling_task, reminder_task) if task), return_exceptions=True)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(run())
