from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import Settings
from app.content import CONTENT, TOPICS
from app.database import Database
from app.security import telegram_user_id, yookassa_source_is_allowed, yoomoney_signature_is_valid
from app.yookassa import YooKassaClient, YooKassaError

logger = logging.getLogger(__name__)


class TelegramRequest(BaseModel):
    init_data: str = Field(min_length=1, max_length=8192)


def create_app(settings: Settings, database: Database, bot=None) -> FastAPI:
    app = FastAPI(title="Pathology Mini App", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory="web"), name="static")
    yookassa = YooKassaClient(settings)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError):
        return JSONResponse(status_code=422, content={"detail": "Invalid request"})

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception):
        logger.exception("Unhandled API error at %s", request.url.path)
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    def authorized_user(init_data: str) -> int:
        return telegram_user_id(init_data, settings.bot_token)

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse("web/index.html", headers={"Cache-Control": "no-store"})

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"ok": True}

    @app.post("/api/session")
    async def session(data: TelegramRequest):
        subscription_end = await database.subscription_end(authorized_user(data.init_data))
        return {"is_active": subscription_end is not None, "subscription_end": subscription_end}

    @app.post("/api/topics")
    async def topics(data: TelegramRequest):
        if not await database.subscription_end(authorized_user(data.init_data)):
            raise HTTPException(403, "Subscription required")
        return {"topics": TOPICS}

    @app.post("/api/content/{topic_id}")
    async def content(topic_id: str, data: TelegramRequest):
        if not await database.subscription_end(authorized_user(data.init_data)):
            raise HTTPException(403, "Subscription required")
        text = CONTENT.get(topic_id)
        if text is None:
            raise HTTPException(404, "Topic not found")
        title = next(topic["title"] for topic in TOPICS if topic["id"] == topic_id)
        return {"title": title, "content": text}

    @app.post("/api/payment/webhook", status_code=200)
    async def payment_webhook(request: Request):
        # YooKassa does not sign webhooks: network allowlist plus GET /payments/{id}
        # is the authenticity check. X-Real-IP is trusted only behind the supplied nginx.
        source = request.headers.get("x-real-ip") if settings.trust_proxy_headers else (request.client.host if request.client else None)
        if not yookassa_source_is_allowed(source):
            logger.warning("Rejected YooKassa webhook from %s", source)
            raise HTTPException(403, "Unexpected webhook source")
        try:
            payload = await request.json()
            event, webhook_payment = payload["event"], payload["object"]
            payment_id, webhook_status = str(webhook_payment["id"]), str(webhook_payment["status"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, "Invalid YooKassa webhook")
        expected_event = f"payment.{webhook_status}"
        if event != expected_event or webhook_status not in {"pending", "waiting_for_capture", "succeeded", "canceled"}:
            raise HTTPException(400, "Unexpected YooKassa event")
        try:
            payment = await yookassa.get_payment(payment_id)
        except YooKassaError as exc:
            raise HTTPException(503, str(exc)) from exc
        if payment.status != webhook_status:
            raise HTTPException(409, "Outdated YooKassa webhook")
        result = await database.update_yookassa_status(payment.id, payment.status, payment.amount, payment.currency, payment.metadata, settings.subscription_days)
        if result is None:
            logger.error("YooKassa payment verification failed for %s", payment.id)
            raise HTTPException(400, "Unknown or mismatched payment")
        if result.activated and bot:
            try:
                await bot.send_message(result.user_id, "Оплата подтверждена. Подписка продлена на 30 дней — приложение уже доступно.")
            except Exception:
                logger.exception("Cannot send payment confirmation to Telegram user %s", result.user_id)
        return {"ok": True}

    @app.post("/api/yoomoney/notification", status_code=200, include_in_schema=False)
    async def yoomoney_notification(request: Request):
        """Legacy endpoint. It is deliberately disabled unless migration fallback is enabled."""
        if not settings.enable_legacy_yoomoney or not settings.yoomoney_notification_secret:
            raise HTTPException(404, "Legacy payment flow disabled")
        form = {key: str(value) for key, value in (await request.form()).items()}
        if not yoomoney_signature_is_valid(form, settings.yoomoney_notification_secret):
            raise HTTPException(400, "Invalid YooMoney signature")
        try:
            amount = Decimal(form.get("amount", "0"))
        except InvalidOperation:
            raise HTTPException(400, "Invalid amount")
        if form.get("notification_type") not in {"p2p-incoming", "card-incoming"} or form.get("currency") != "643" or form.get("codepro") == "true" or form.get("unaccepted") == "true" or amount != settings.subscription_price:
            raise HTTPException(400, "Unexpected payment")
        result = await database.activate_legacy_payment(form.get("label", ""), form.get("operation_id", ""), settings.subscription_days)
        if result is None:
            raise HTTPException(404, "Unknown payment")
        return {"ok": True}

    return app
