from __future__ import annotations

import logging
import asyncio
import time
import hashlib
from collections import OrderedDict, deque
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qsl, urlsplit

import aiosqlite
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from app.config import Settings
from app.access import AccessControl
from app.database import Database
from app.documents import DocumentStorage, InvalidPDF
from app.media import InvalidImage, MediaStorage
from app.security import telegram_user_id, yookassa_source_is_allowed, yoomoney_signature_is_valid
from app.proofs import digest, verify_proof
from app.yookassa import YooKassaClient, YooKassaError

logger = logging.getLogger(__name__)


class TelegramRequest(BaseModel):
    init_data: str = Field(min_length=1, max_length=8192)


class CategoryInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    title: str = Field(min_length=1, max_length=200)


class MaterialInput(CategoryInput):
    title: str = Field(min_length=1, max_length=300)
    category_id: int = Field(gt=0)
    text: str = Field(default="", max_length=200_000)


class ViewInput(BaseModel):
    view: str = Field(min_length=32, max_length=128)


class TicketInput(ViewInput):
    kind: Literal["image", "document"]
    resource_id: int = Field(gt=0)
    page: int = Field(default=1, gt=0)


def create_app(settings: Settings, database: Database, bot=None) -> FastAPI:
    app = FastAPI(title="Pathology Mini App", docs_url=None, redoc_url=None, openapi_url=None)
    web_directory = Path(__file__).resolve().parent.parent / "web"
    # Content hashes invalidate previously cached clients when the access
    # protocol changes; stale JS must never silently downgrade authorization.
    shell = (web_directory / "index.html").read_text()
    for name in ("app.js", "styles.css", "auth.js"):
        version = hashlib.sha256((web_directory / name).read_bytes()).hexdigest()[:16]
        shell = shell.replace(f"/static/{name}", f"/static/{name}?v={version}")
    app.mount("/static", StaticFiles(directory=web_directory), name="static")
    yookassa = YooKassaClient(settings)
    media = MediaStorage(settings.media_path)
    documents = DocumentStorage(settings.media_path)
    image_workers = asyncio.Semaphore(2)
    document_worker = asyncio.Semaphore(1)
    document_uploads = asyncio.Semaphore(2)
    image_requests: OrderedDict[int, deque[float]] = OrderedDict()
    access = AccessControl(database, settings)
    public = urlsplit(settings.public_base_url)
    public_origin = f"{public.scheme}://{public.netloc}"

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/") or request.url.path == "/":
            response.headers["Cache-Control"] = "private, no-store, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Vary"] = "Authorization"
        elif request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), display-capture=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' https://telegram.org; "
            "style-src 'self'; img-src 'self' blob:; connect-src 'self'; "
            "object-src 'none'; base-uri 'none'; form-action 'none'; worker-src 'none'; "
            "frame-ancestors https://web.telegram.org https://*.telegram.org"
        )
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError):
        return JSONResponse(status_code=422, content={"detail": "Invalid request"})

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception):
        logger.exception("Unhandled API error at %s", request.url.path)
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    def proof(request: Request, token: str | None = None):
        return verify_proof(request.headers.get("dpop", ""), request.method,
                            public_origin + request.url.path, time.time(), token=token)

    async def request_user(request: Request) -> int:
        scheme, _, credentials = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "dpop" or not 32 <= len(credentials) <= 128:
            raise HTTPException(401, "Откройте приложение заново из Telegram.")
        session = await access.authenticate(credentials, proof(request, credentials))
        request.state.reader_session = session
        return session.user_id

    async def reader(user_id: int = Depends(request_user)) -> int:
        if user_id not in settings.admin_ids and not await database.subscription_end(user_id):
            raise HTTPException(403, "Нужна активная подписка.")
        return user_id

    def admin(user_id: int = Depends(request_user)) -> int:
        if user_id not in settings.admin_ids:
            raise HTTPException(403, "Нужны права администратора.")
        return user_id

    @app.get("/", include_in_schema=False)
    async def index():
        return HTMLResponse(shell, headers={"Cache-Control": "no-store"})

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"ok": True}

    async def session_info(user_id: int):
        subscription_end = await database.subscription_end(user_id)
        return {"is_active": subscription_end is not None, "subscription_end": subscription_end,
                "is_admin": user_id in settings.admin_ids, "user_id": user_id,
                "price": f"{settings.subscription_price:.2f}", "days": settings.subscription_days,
                "max_image_bytes": settings.max_image_bytes, "max_pdf_bytes": settings.max_pdf_bytes}

    @app.get("/api/session/clock")
    async def session_clock():
        return {"server_time": time.time()}

    @app.post("/api/session")
    async def session(data: TelegramRequest, request: Request):
        user_id = telegram_user_id(data.init_data, settings.bot_token, max_age=300)
        launch_hash = digest(dict(parse_qsl(data.init_data))["hash"])
        token, expires = await access.create_session(user_id, launch_hash, proof(request))
        return {**await session_info(user_id), "access_token": token, "session_expires": expires, "server_time": time.time()}

    @app.get("/api/session")
    async def current_session(request: Request, user_id: int = Depends(request_user)):
        return {**await session_info(user_id), "session_expires": request.state.reader_session.expires, "server_time": time.time()}

    @app.get("/api/categories", dependencies=[Depends(reader)])
    async def categories():
        return {"categories": await database.categories()}

    @app.get("/api/categories/{category_id}/materials", dependencies=[Depends(reader)])
    async def materials(category_id: int):
        category = await database.category(category_id)
        if category is None:
            raise HTTPException(404, "Раздел не найден.")
        return {"category": category, "materials": await database.materials(category_id)}

    @app.get("/api/materials/{material_id}", dependencies=[Depends(reader)])
    async def material(material_id: int, request: Request):
        result = await database.material(material_id)
        if result is None:
            raise HTTPException(404, "Материал не найден.")
        result["view_token"] = await access.open_view(request.state.reader_session, material_id)
        return result

    @app.post("/api/reader/ticket", dependencies=[Depends(reader)])
    async def page_ticket(data: TicketInput, request: Request):
        ticket = await access.issue_ticket(request.state.reader_session, data.view, data.kind, data.resource_id, data.page)
        return {"ticket": ticket, "expires_in": 30}

    @app.post("/api/reader/heartbeat", dependencies=[Depends(reader)])
    async def reader_heartbeat(data: ViewInput, request: Request):
        await access.heartbeat(request.state.reader_session, data.view)
        return {"ok": True}

    @app.post("/api/reader/close", dependencies=[Depends(request_user)])
    async def reader_close(data: ViewInput, request: Request):
        await access.close_view(request.state.reader_session, data.view)
        return {"ok": True}

    @app.get("/api/admin/security/events", dependencies=[Depends(admin)])
    async def security_events():
        return {"events": await access.audit()}

    @app.post("/api/admin/security/users/{user_id}/{action}", dependencies=[Depends(admin)])
    async def manage_access(user_id: int, action: Literal["revoke", "unblock"]):
        if user_id <= 0:
            raise HTTPException(422, "Некорректный Telegram ID.")
        await access.manage_user(user_id, action)
        return {"ok": True}

    def limit_page_requests(user_id: int):
        now = time.monotonic()
        history = image_requests.setdefault(user_id, deque())
        image_requests.move_to_end(user_id)
        while history and history[0] <= now - 60:
            history.popleft()
        if len(history) >= 60:
            raise HTTPException(429, "Слишком много запросов страниц. Подождите минуту.", headers={"Retry-After": "60"})
        history.append(now)
        if len(image_requests) > 10_000:
            image_requests.popitem(last=False)

    @app.get("/api/images/{image_id}")
    async def material_image(image_id: int, request: Request, user_id: int = Depends(reader)):
        limit_page_requests(user_id)
        record = await database.image(image_id)
        if record is None:
            raise HTTPException(404, "Фото не найдено.")
        view = request.headers.get("x-read-view", "")
        await access.consume_ticket(request.state.reader_session, view, request.headers.get("x-page-ticket", ""), f"image:{image_id}:1")
        try:
            async with image_workers:
                photo = await run_in_threadpool(media.render, record["filename"])
        except FileNotFoundError:
            raise HTTPException(404, "Фото не найдено.")
        except InvalidImage as exc:
            raise HTTPException(422, str(exc)) from exc
        await access.check_delivery(request.state.reader_session, view)
        return Response(photo, media_type="image/jpeg")

    @app.get("/api/documents/{document_id}/pages/{page_number}")
    async def document_page(document_id: int, page_number: int, request: Request, user_id: int = Depends(reader)):
        limit_page_requests(user_id)
        record = await database.document(document_id)
        if record is None or not 1 <= page_number <= len(record["page_sizes"]):
            raise HTTPException(404, "Страница PDF не найдена.")
        view = request.headers.get("x-read-view", "")
        await access.consume_ticket(request.state.reader_session, view, request.headers.get("x-page-ticket", ""), f"document:{document_id}:{page_number}")
        try:
            async with document_worker:
                # Access may expire while waiting for another page to render.
                await access.check_delivery(request.state.reader_session, view)
                page = await run_in_threadpool(documents.render, record["filename"], page_number - 1)
        except (FileNotFoundError, IndexError):
            raise HTTPException(404, "Страница PDF не найдена.")
        except InvalidPDF as exc:
            raise HTTPException(422, str(exc)) from exc
        await access.check_delivery(request.state.reader_session, view)
        return Response(page, media_type="image/jpeg")

    @app.post("/api/admin/categories", status_code=201, dependencies=[Depends(admin)])
    async def create_category(data: CategoryInput):
        return {"id": await database.save_category(data.title)}

    @app.patch("/api/admin/categories/{category_id}", dependencies=[Depends(admin)])
    async def update_category(category_id: int, data: CategoryInput):
        if await database.save_category(data.title, category_id) is None:
            raise HTTPException(404, "Раздел не найден.")
        return {"id": category_id}

    async def store_material(data: MaterialInput, material_id: int | None = None):
        try:
            result = await database.save_material(data.category_id, data.title, data.text, material_id)
        except aiosqlite.IntegrityError:
            raise HTTPException(404, "Раздел не найден.")
        if result is None:
            raise HTTPException(404, "Материал не найден.")
        return {"id": result}

    @app.post("/api/admin/materials", status_code=201, dependencies=[Depends(admin)])
    async def create_material(data: MaterialInput):
        return await store_material(data)

    @app.patch("/api/admin/materials/{material_id}", dependencies=[Depends(admin)])
    async def update_material(material_id: int, data: MaterialInput):
        return await store_material(data, material_id)

    @app.post("/api/admin/materials/{material_id}/images", status_code=201, dependencies=[Depends(admin)])
    async def upload_image(material_id: int, request: Request):
        if await database.material(material_id) is None:
            raise HTTPException(404, "Материал не найден.")
        # Read raw file bytes only after authentication, with an enforced limit
        # even for chunked requests or a forged/missing Content-Length.
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > settings.max_image_bytes:
                raise HTTPException(413, "Размер фото не должен превышать 10 МБ.")
            data.extend(chunk)
        try:
            async with image_workers:
                filename = await run_in_threadpool(media.save, bytes(data))
        except InvalidImage as exc:
            raise HTTPException(422, str(exc)) from exc
        try:
            image_id = await database.add_image(material_id, filename)
        except BaseException as exc:
            await run_in_threadpool(media.delete, filename)
            if isinstance(exc, aiosqlite.IntegrityError):
                raise HTTPException(404, "Материал был удалён.") from exc
            raise
        return {"id": image_id}

    @app.post("/api/admin/materials/{material_id}/documents", status_code=201, dependencies=[Depends(admin)])
    async def upload_document(material_id: int, request: Request, title: str = "Документ.pdf"):
        title = title.strip()
        if not title or len(title) > 300:
            raise HTTPException(422, "Название PDF должно содержать от 1 до 300 символов.")
        if await database.material(material_id) is None:
            raise HTTPException(404, "Материал не найден.")
        size_header = request.headers.get("content-length")
        if size_header and size_header.isdigit() and int(size_header) > settings.max_pdf_bytes:
            raise HTTPException(413, f"Размер PDF не должен превышать {settings.max_pdf_bytes // (1024 * 1024)} МБ.")
        # The body is written incrementally, including chunked requests. A large
        # PDF never becomes a full in-memory request buffer.
        async with document_uploads:
            filename, target = await run_in_threadpool(documents.create_upload)
            stored = False
            try:
                size = 0
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > settings.max_pdf_bytes:
                        raise HTTPException(413, f"Размер PDF не должен превышать {settings.max_pdf_bytes // (1024 * 1024)} МБ.")
                    await run_in_threadpool(target.write, chunk)
                await run_in_threadpool(target.close)
                async with document_worker:
                    page_sizes = await run_in_threadpool(documents.inspect, filename)
                document_id = await database.add_document(material_id, filename, title, size, page_sizes)
                stored = True
                return {"id": document_id, "page_count": len(page_sizes)}
            except InvalidPDF as exc:
                raise HTTPException(422, str(exc)) from exc
            except aiosqlite.IntegrityError as exc:
                raise HTTPException(404, "Материал был удалён.") from exc
            finally:
                await run_in_threadpool(target.close)
                if not stored:
                    await run_in_threadpool(documents.delete, filename)

    async def remove_content(kind: str, content_id: int):
        filenames = await database.delete_content(kind, content_id)
        if filenames is None:
            raise HTTPException(404, "Запись не найдена.")
        for filename in filenames:
            try:
                storage = documents if filename.endswith(".pdf") else media
                await run_in_threadpool(storage.delete, filename)
            except OSError:
                # DB deletion immediately revokes access even if disk cleanup fails.
                logger.exception("Could not remove private attachment %s", filename)
        return {"ok": True}

    @app.delete("/api/admin/categories/{category_id}", dependencies=[Depends(admin)])
    async def delete_category(category_id: int):
        return await remove_content("category", category_id)

    @app.delete("/api/admin/materials/{material_id}", dependencies=[Depends(admin)])
    async def delete_material(material_id: int):
        return await remove_content("material", material_id)

    @app.delete("/api/admin/images/{image_id}", dependencies=[Depends(admin)])
    async def delete_image(image_id: int):
        return await remove_content("image", image_id)

    @app.delete("/api/admin/documents/{document_id}", dependencies=[Depends(admin)])
    async def delete_document(document_id: int):
        return await remove_content("document", document_id)

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
                await bot.send_message(result.user_id, f"Оплата подтверждена. Подписка продлена на {settings.subscription_days} дней — приложение уже доступно.")
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
