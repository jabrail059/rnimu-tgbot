from __future__ import annotations

from decimal import Decimal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import Settings
from app.content import CONTENT, TOPICS
from app.database import Database
from app.security import telegram_user_id, yoomoney_signature_is_valid


def create_app(settings: Settings, database: Database, bot=None) -> FastAPI:
    app = FastAPI(title="Pathology Mini App")
    app.mount("/static", StaticFiles(directory="web"), name="static")

    def authorized_user(init_data: str) -> int:
        return telegram_user_id(init_data, settings.bot_token)

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse("web/index.html")

    @app.post("/api/session")
    async def session(request: Request):
        data = await request.json()
        user_id = authorized_user(data.get("init_data", ""))
        subscription_end = await database.subscription_end(user_id)
        return {"is_active": subscription_end is not None, "subscription_end": subscription_end}

    @app.post("/api/topics")
    async def topics(request: Request):
        data = await request.json()
        user_id = authorized_user(data.get("init_data", ""))
        if not await database.subscription_end(user_id):
            raise HTTPException(403, "Subscription required")
        return {"topics": TOPICS}

    @app.post("/api/content/{topic_id}")
    async def content(topic_id: str, request: Request):
        data = await request.json()
        user_id = authorized_user(data.get("init_data", ""))
        if not await database.subscription_end(user_id):
            raise HTTPException(403, "Subscription required")
        text = CONTENT.get(topic_id)
        if text is None:
            raise HTTPException(404, "Topic not found")
        return {"title": next(topic["title"] for topic in TOPICS if topic["id"] == topic_id), "content": text}

    @app.post("/api/yoomoney/notification", status_code=200)
    async def yoomoney_notification(request: Request):
        form = {key: str(value) for key, value in (await request.form()).items()}
        if not yoomoney_signature_is_valid(form, settings.yoomoney_notification_secret):
            raise HTTPException(400, "Invalid YooMoney signature")
        notification_type = form.get("notification_type", "")
        operation_id = form.get("operation_id", "")
        amount = form.get("amount", "0")
        currency = form.get("currency", "")
        label = form.get("label", "")
        codepro = form.get("codepro", "")
        unaccepted = form.get("unaccepted", "")
        if notification_type not in {"p2p-incoming", "card-incoming"} or currency != "643":
            raise HTTPException(400, "Unexpected payment")
        if codepro == "true" or unaccepted == "true" or Decimal(amount) < Decimal("120.00"):
            raise HTTPException(400, "Payment not accepted")
        user_id = await database.activate_payment(label, operation_id)
        if user_id is None:
            raise HTTPException(404, "Unknown payment label")
        if bot:
            await bot.send_message(user_id, "Оплата получена. Подписка продлена на 30 дней — приложение уже доступно.")
        return {"ok": True}

    return app
