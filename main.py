import asyncio
import logging
import uuid

import uvicorn
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo
from yoomoney import Quickpay
from aiogram.client.session.aiohttp import AiohttpSession

from app.config import get_settings
from app.database import Database
from app.web import create_app

logging.basicConfig(level=logging.INFO)
settings = get_settings()
database = Database(settings.database_path)
session = AiohttpSession(
    proxy="socks5://127.0.0.1:10808"
)

bot = Bot(
    settings.bot_token,
    session=session
)
dp = Dispatcher()


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Купить / продлить — 120 ₽", callback_data="buy")],
        [InlineKeyboardButton(text="Открыть приложение", web_app=WebAppInfo(url=settings.public_base_url))],
    ])


@dp.message(CommandStart())
async def start(message: Message) -> None:
    await database.upsert_user(message.from_user.id, message.from_user.username)
    await message.answer(
        "Патанатомия: полный доступ к темам в Mini App. Стоимость — 120 ₽ на 30 дней.",
        reply_markup=main_keyboard(),
    )


@dp.callback_query(F.data == "buy")
async def buy(callback: CallbackQuery) -> None:
    user = callback.from_user
    await database.upsert_user(user.id, user.username)
    payment_id = f"pathology-{uuid.uuid4().hex}"
    await database.create_payment(payment_id, user.id)
    quickpay = Quickpay(
        receiver=settings.yoomoney_wallet,
        quickpay_form="shop",
        targets="Подписка на материалы по патанатомии (30 дней)",
        paymentType="AC",
        sum=120,
        label=payment_id,
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Перейти к оплате", url=quickpay.redirected_url)]])
    await callback.message.answer("После оплаты доступ откроется автоматически. Не закрывайте бот: я пришлю подтверждение.", reply_markup=keyboard)
    await callback.answer()


async def run() -> None:
    await database.initialize()
    web_app = create_app(settings, database, bot)
    server = uvicorn.Server(uvicorn.Config(web_app, host="0.0.0.0", port=8000, log_level="info"))
    server_task = asyncio.create_task(server.serve())
    try:
        await dp.start_polling(bot)
    finally:
        server.should_exit = True
        await server_task
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(run())
