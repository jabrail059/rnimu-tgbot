from __future__ import annotations

import logging
import os
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import Settings
from app.database import Database
from app.security import telegram_user_id, yookassa_source_is_allowed, yoomoney_signature_is_valid
from app.yookassa import YooKassaClient, YooKassaError

logger = logging.getLogger(__name__)

MAX_IMAGE_SIZE = 12 * 1024 * 1024
IMAGE_SIGNATURES = {
    b"\x89PNG\r\n\x1a\n": ("png", "image/png"),
    b"\xff\xd8\xff": ("jpg", "image/jpeg"),
    b"RIFF": ("webp", "image/webp"),
}


class TelegramRequest(BaseModel):
    init_data: str = Field(min_length=1, max_length=8192)


class CategoryIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class MaterialIn(BaseModel):
    category_id: int = Field(gt=0)
    title: str = Field(min_length=1, max_length=300)
    text: str = Field(default="", max_length=200_000)


def create_app(settings: Settings, database: Database, bot=None) -> FastAPI:
    app = FastAPI(title="Pathology Mini App", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory="web"), name="static")
    yookassa = YooKassaClient(settings)
    media_dir = Path(settings.media_dir)
    media_dir.mkdir(parents=True, exist_ok=True)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError):
        return JSONResponse(status_code=422, content={"detail": "Invalid request"})

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception):
        logger.exception("Unhandled API error at %s", request.url.path)
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    def authorized_user(init_data: str) -> int:
        return telegram_user_id(init_data, settings.bot_token)

    async def admin_user(init_data: str) -> int:
        user_id = authorized_user(init_data)
        if settings.admin_user_ids and user_id in settings.admin_user_ids:
            return user_id
        if await database.is_admin(user_id):
            return user_id
        raise HTTPException(403, "Admin access required")

    def ensure_category(category_id: int, categories: list[dict]) -> None:
        if not any(category["id"] == category_id for category in categories):
            raise HTTPException(400, "Category not found")

    @app.on_event("startup")
    async def startup() -> None:
        if settings.admin_user_ids:
            await database.sync_admins(list(settings.admin_user_ids))

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse("web/index.html", headers={"Cache-Control": "no-store"})

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"ok": True}

    @app.post("/api/session")
    async def session(data: TelegramRequest):
        user_id = authorized_user(data.init_data)
        subscription_end = await database.subscription_end(user_id)
        is_admin = user_id in settings.admin_user_ids or await database.is_admin(user_id)
        return {
            "is_active": subscription_end is not None,
            "subscription_end": subscription_end,
            "is_admin": is_admin,
        }

    @app.post("/api/topics")
    async def topics(data: TelegramRequest):
        if not await database.subscription_end(authorized_user(data.init_data)):
            raise HTTPException(403, "Subscription required")
        categories = await database.list_categories()
        return {"topics": [
            {"id": str(category["id"]), "title": category["title"], "description": ""}
            for category in categories
        ]}

    @app.post("/api/materials/{category_id}")
    async def materials(category_id: int, data: TelegramRequest):
        if not await database.subscription_end(authorized_user(data.init_data)):
            raise HTTPException(403, "Subscription required")
        category = next((c for c in await database.list_categories() if c["id"] == category_id), None)
        if not category:
            raise HTTPException(404, "Category not found")
        items = await database.list_materials(category_id)
        return {"category": {"id": category["id"], "title": category["title"]},
                "materials": [{"id": x["id"], "title": x["title"], "text": x["text"][:140]} for x in items]}

    @app.post("/api/content/{material_id}")
    async def content(material_id: int, data: TelegramRequest):
        if not await database.subscription_end(authorized_user(data.init_data)):
            raise HTTPException(403, "Subscription required")
        material = await database.get_material(material_id)
        if not material:
            raise HTTPException(404, "Material not found")
        return {
            "id": material["id"],
            "title": material["title"],
            "content": material["text"],
            "images": [{"id": image["id"]} for image in material["images"]],
        }

    @app.post("/api/image/{image_id}")
    async def image(image_id: int, data: TelegramRequest):
        if not await database.subscription_end(authorized_user(data.init_data)):
            raise HTTPException(403, "Subscription required")
        filename = await database.material_image_filename(image_id)
        if not filename:
            raise HTTPException(404, "Image not found")
        path = media_dir / filename
        if not path.is_file() or path.parent != media_dir:
            raise HTTPException(404, "Image not found")
        return Response(
            content=path.read_bytes(),
            media_type=_mime_for_path(path),
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        )

    # ------------------------- Admin API -------------------------

    @app.post("/api/admin/categories")
    async def admin_categories(data: TelegramRequest):
        await admin_user(data.init_data)
        return {"categories": await database.list_categories()}

    @app.post("/api/admin/categories/create")
    async def admin_create_category(payload: CategoryIn, data: TelegramRequest):
        await admin_user(data.init_data)
        return {"id": await database.create_category(payload.title.strip())}

    @app.post("/api/admin/categories/{category_id}/delete")
    async def admin_delete_category(category_id: int, data: TelegramRequest):
        await admin_user(data.init_data)
        materials = await database.list_materials(category_id)
        for material in materials:
            for filename in await database.delete_material(material["id"]):
                _safe_unlink(media_dir / filename)
        await database.delete_category(category_id)
        return {"ok": True}

    @app.post("/api/admin/materials")
    async def admin_materials(data: TelegramRequest):
        await admin_user(data.init_data)
        return {"materials": await database.list_materials()}

    @app.post("/api/admin/materials/create")
    async def admin_create_material(payload: MaterialIn, data: TelegramRequest):
        await admin_user(data.init_data)
        ensure_category(payload.category_id, await database.list_categories())
        material_id = await database.create_material(
            payload.category_id, payload.title.strip(), payload.text
        )
        return {"id": material_id}

    @app.post("/api/admin/materials/{material_id}")
    async def admin_material(material_id: int, data: TelegramRequest):
        await admin_user(data.init_data)
        material = await database.get_material(material_id)
        if not material:
            raise HTTPException(404, "Material not found")
        return material

    @app.post("/api/admin/materials/{material_id}/update")
    async def admin_update_material(material_id: int, payload: MaterialIn, data: TelegramRequest):
        await admin_user(data.init_data)
        ensure_category(payload.category_id, await database.list_categories())
        if not await database.update_material(material_id, payload.category_id, payload.title.strip(), payload.text):
            raise HTTPException(404, "Material not found")
        return {"ok": True}

    @app.post("/api/admin/materials/{material_id}/delete")
    async def admin_delete_material(material_id: int, data: TelegramRequest):
        await admin_user(data.init_data)
        filenames = await database.delete_material(material_id)
        if not filenames:
            raise HTTPException(404, "Material not found")
        for filename in filenames:
            _safe_unlink(media_dir / filename)
        return {"ok": True}

    @app.post("/api/admin/materials/{material_id}/images")
    async def admin_upload_images(
        material_id: int,
        data: str = Form(..., alias="init_data"),
        files: list[UploadFile] = File(...),
    ):
        await admin_user(data)
        material = await database.get_material(material_id)
        if not material:
            raise HTTPException(404, "Material not found")
        position = len(material["images"])
        uploaded = []
        for upload in files:
            if not upload.filename:
                continue
            raw = await upload.read(MAX_IMAGE_SIZE + 1)
            if len(raw) > MAX_IMAGE_SIZE:
                raise HTTPException(413, "Image is too large")
            ext, mime = _detect_image(raw)
            filename = f"{uuid.uuid4().hex}.{ext}"
            (media_dir / filename).write_bytes(raw)
            image_id = await database.add_material_image(material_id, filename, position)
            position += 1
            uploaded.append({"id": image_id, "mime": mime})
        return {"images": uploaded}

    @app.post("/api/admin/images/{image_id}/delete")
    async def admin_delete_image(image_id: int, data: TelegramRequest):
        await admin_user(data.init_data)
        filename = await database.delete_material_image(image_id)
        if not filename:
            raise HTTPException(404, "Image not found")
        _safe_unlink(media_dir / filename)
        return {"ok": True}

    # ------------------------- Payments -------------------------

    @app.post("/api/payment/webhook", status_code=200)
    async def payment_webhook(request: Request):
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
                await bot.send_message(result.user_id, f"Оплата подтверждена. Подписка продлена на {settings.subscription_days} дней — приложение уже доступно.")
            except Exception:
                logger.exception("Cannot send payment confirmation to Telegram user %s", result.user_id)
        return {"ok": True}

    @app.post("/api/yoomoney/notification", status_code=200, include_in_schema=False)
    async def yoomoney_notification(request: Request):
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


def _safe_unlink(path: Path) -> None:
    try:
        path.resolve().relative_to(path.parent.resolve())
    except ValueError:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Cannot delete media file %s", path)


def _detect_image(raw: bytes) -> tuple[str, str]:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "jpg", "image/jpeg"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp", "image/webp"
    raise HTTPException(415, "Поддерживаются только PNG, JPG и WEBP")


def _mime_for_path(path: Path) -> str:
    suffix = path.suffix.lower()
    return {".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp"}.get(suffix, "application/octet-stream")
