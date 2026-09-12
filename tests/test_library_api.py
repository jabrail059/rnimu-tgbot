import asyncio
import hashlib
import hmac
import io
import json
import sqlite3
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.config import Settings
from app.database import Database
from app.media import MediaStorage
from app.web import create_app
from app.yookassa import YooKassaClient, YooKassaPayment

TOKEN = "123456:unit-test-token"


def signed_data(user_id, age=0):
    fields = {"auth_date": str(int(time.time()) - age), "user": json.dumps({"id": user_id})}
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, "\n".join(f"{k}={v}" for k, v in sorted(fields.items())).encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def auth(user_id, age=0):
    return {"Authorization": f"tma {signed_data(user_id, age)}"}


def photo_bytes():
    image = Image.new("RGB", (800, 600), "#ccbbbb")
    output = io.BytesIO()
    exif = Image.Exif(); exif[270] = "PRIVATE METADATA"
    image.save(output, "JPEG", exif=exif)
    return output.getvalue()


@pytest.fixture
def project(tmp_path):
    settings = Settings(bot_token=TOKEN, public_base_url="https://example.com", database_path=str(tmp_path / "app.db"),
                        yookassa_shop_id="test", yookassa_secret_key="test", subscription_price=Decimal("120.00"),
                        subscription_days=30, proxy_url=None, trust_proxy_headers=False, enable_legacy_yoomoney=False,
                        yoomoney_wallet=None, yoomoney_notification_secret=None, admin_ids=frozenset({1}),
                        media_path=str(tmp_path / "photos"))
    db = Database(settings.database_path)
    asyncio.run(db.initialize())
    for uid in (1, 2, 3):
        asyncio.run(db.upsert_user(uid, f"user{uid}"))
    with sqlite3.connect(settings.database_path) as connection:
        connection.execute("UPDATE users SET subscription_end=? WHERE user_id=2", ((datetime.now(UTC) + timedelta(days=10)).isoformat(),))
    with TestClient(create_app(settings, db)) as client:
        yield client, db, settings


def create_material(client):
    category = client.post("/api/admin/categories", headers=auth(1), json={"title": "Воспаление"})
    assert category.status_code == 201
    material = client.post("/api/admin/materials", headers=auth(1), json={"category_id": category.json()["id"], "title": "Острое воспаление", "text": "Первая строка\n<script>alert(1)</script>"})
    assert material.status_code == 201
    return category.json()["id"], material.json()["id"]


def test_session_and_read_access(project):
    client, _, _ = project
    assert client.post("/api/session", json={"init_data": signed_data(1)}).json()["is_admin"]
    subscriber = client.post("/api/session", json={"init_data": signed_data(2)}).json()
    assert subscriber["is_active"] and not subscriber["is_admin"] and subscriber["price"] == "120.00"
    assert client.get("/api/categories").status_code == 401
    assert client.get("/api/categories", headers=auth(3)).status_code == 403
    for uid in (1, 2):
        assert client.get("/api/categories", headers=auth(uid)).status_code == 200
    for age in (3601, -120):
        assert client.get("/api/categories", headers=auth(1, age)).status_code == 401
    assert client.get("/api/categories", headers={"Authorization": auth(3)["Authorization"].replace("%3A+3", "%3A+1")}).status_code == 401
    assert client.get("/api/categories?init_data=" + signed_data(1)).status_code == 401


@pytest.mark.parametrize("method,path,body", [
    ("post", "/api/admin/categories", {"title": "X"}), ("patch", "/api/admin/categories/1", {"title": "X"}),
    ("delete", "/api/admin/categories/1", None), ("post", "/api/admin/materials", {"category_id": 1, "title": "X"}),
    ("patch", "/api/admin/materials/1", {"category_id": 1, "title": "X"}), ("delete", "/api/admin/materials/1", None),
    ("post", "/api/admin/materials/1/images", None), ("delete", "/api/admin/images/1", None),
])
def test_all_mutations_require_admin(project, method, path, body):
    client, _, _ = project
    for uid in (2, 3):
        assert client.request(method, path, headers=auth(uid), json=body).status_code == 403
    assert client.request(method, path, json=body).status_code == 401


def test_crud_and_cascading_file_deletion(project):
    client, db, settings = project
    category_id, material_id = create_material(client)
    assert client.patch(f"/api/admin/categories/{category_id}", headers=auth(1), json={"title": "Изменено"}).status_code == 200
    response = client.get(f"/api/categories/{category_id}/materials", headers=auth(2)).json()
    assert response["category"]["title"] == "Изменено" and response["materials"][0]["id"] == material_id
    images = [client.post(f"/api/admin/materials/{material_id}/images", headers=auth(1), content=photo_bytes()).json()["id"] for _ in range(3)]
    material = client.get(f"/api/materials/{material_id}", headers=auth(2)).json()
    assert [image["id"] for image in material["images"]] == images
    assert "<script>" in material["text"] and "filename" not in json.dumps(material)
    assert client.delete(f"/api/admin/images/{images[1]}", headers=auth(1)).status_code == 200
    assert client.get(f"/api/images/{images[1]}", headers=auth(2)).status_code == 404
    assert len(list(Path(settings.media_path).iterdir())) == 2
    assert client.delete(f"/api/admin/categories/{category_id}", headers=auth(1)).status_code == 200
    assert client.get(f"/api/materials/{material_id}", headers=auth(2)).status_code == 404
    assert not list(Path(settings.media_path).iterdir())
    assert asyncio.run(db.subscription_end(2)) is not None
    with sqlite3.connect(settings.database_path) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_images_private_personalized_and_not_cached(project):
    client, db, settings = project
    _, material_id = create_material(client)
    upload = client.post(f"/api/admin/materials/{material_id}/images", headers=auth(1), content=photo_bytes())
    assert upload.status_code == 201
    image_id = upload.json()["id"]; path = f"/api/images/{image_id}"
    assert client.get(path).status_code == 401
    assert client.get(path, headers=auth(3)).status_code == 403
    first = client.get(path, headers=auth(1)); second = client.get(path, headers=auth(2))
    assert first.status_code == second.status_code == 200 and first.content != second.content
    assert "no-store" in second.headers["cache-control"] and second.headers["content-type"] == "image/jpeg"
    assert second.headers["vary"] == "Authorization" and "etag" not in second.headers
    normalized = Image.open(io.BytesIO(second.content))
    assert normalized.size == (800, 600) and not normalized.getexif()
    record = asyncio.run(db.image(image_id))
    assert client.get(f"/static/{record['filename']}").status_code == 404
    assert client.get(f"/data/material_images/{record['filename']}").status_code == 404
    with sqlite3.connect(settings.database_path) as connection:
        connection.execute("UPDATE users SET subscription_end=? WHERE user_id=2", ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),))
    assert client.get(path, headers=auth(2)).status_code == 403
    assert client.get(f"/api/materials/{material_id}", headers=auth(2)).status_code == 403


def test_invalid_uploads_and_limits(project):
    client, db, settings = project
    _, material_id = create_material(client)
    path = f"/api/admin/materials/{material_id}/images"
    for data in (b"", b"<svg onload='alert(1)'/>", b"not an image"):
        assert client.post(path, headers=auth(1), content=data).status_code == 422
    with TestClient(create_app(replace(settings, max_image_bytes=100), db)) as limited:
        assert limited.post(path, headers=auth(1), content=photo_bytes()).status_code == 413
        assert limited.post(path, headers=auth(1), content=iter([b"x" * 60, b"y" * 60])).status_code == 413
    assert not list(Path(settings.media_path).iterdir())
    assert asyncio.run(db.material(material_id))["images"] == []


def test_image_upload_rollback_removes_file(project, monkeypatch):
    client, db, settings = project
    _, material_id = create_material(client)
    monkeypatch.setattr(db, "add_image", AsyncMock(side_effect=sqlite3.IntegrityError()))
    assert client.post(f"/api/admin/materials/{material_id}/images", headers=auth(1), content=photo_bytes()).status_code == 404
    assert not list(Path(settings.media_path).iterdir())


def test_validation_and_missing_records(project):
    client, _, _ = project
    for title in ("  ", "x" * 161):
        assert client.post("/api/admin/categories", headers=auth(1), json={"title": title}).status_code == 422
    assert client.post("/api/admin/materials", headers=auth(1), json={"category_id": 999, "title": "X"}).status_code == 404
    for path in ("/api/materials/999", "/api/images/999", "/api/categories/999/materials"):
        assert client.get(path, headers=auth(2)).status_code == 404
    assert client.delete("/api/admin/materials/999", headers=auth(1)).status_code == 404
    assert client.patch("/api/admin/categories/999", headers=auth(1), json={"title": "X"}).status_code == 404


def test_material_edit_delete_and_server_rate_limit(project):
    client, _, _ = project
    category_id, material_id = create_material(client)
    assert client.patch(f"/api/admin/materials/{material_id}", headers=auth(1), json={"category_id": category_id, "title": "Обновлено", "text": "Новый текст"}).status_code == 200
    assert client.get(f"/api/materials/{material_id}", headers=auth(2)).json()["text"] == "Новый текст"
    for _ in range(60):
        assert client.get("/api/images/999", headers=auth(2)).status_code == 404
    limited = client.get("/api/images/999", headers=auth(2))
    assert limited.status_code == 429 and limited.headers["retry-after"] == "60"
    assert client.delete(f"/api/admin/materials/{material_id}", headers=auth(1)).status_code == 200
    assert client.get(f"/api/categories/{category_id}/materials", headers=auth(1)).json()["materials"] == []


def test_migration_preserves_existing_users_and_payments(tmp_path):
    path = str(tmp_path / "legacy.db"); end = (datetime.now(UTC) + timedelta(days=9)).isoformat()
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE users(user_id INTEGER PRIMARY KEY, username TEXT, subscription_end TEXT, created_at TEXT NOT NULL);
            CREATE TABLE payments(id TEXT PRIMARY KEY, user_id INTEGER, amount TEXT, status TEXT, operation_id TEXT UNIQUE, created_at TEXT, paid_at TEXT);
        """)
        connection.execute("INSERT INTO users VALUES (42, 'existing', ?, '2026-01-01')", (end,))
        connection.execute("INSERT INTO payments VALUES ('old', 42, '120.00', 'success', 'operation', '2026-01-01', '2026-01-01')")
    db = Database(path); asyncio.run(db.initialize()); asyncio.run(db.initialize())
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT username, subscription_end FROM users").fetchone() == ("existing", end)
        assert connection.execute("SELECT id, amount, status, operation_id FROM payments").fetchone() == ("old", "120.00", "succeeded", "operation")
        assert connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [(1,), (2,)]
        assert connection.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 0


def test_yookassa_webhook_still_verifies_and_extends_once(project, monkeypatch):
    client, db, settings = project
    asyncio.run(db.reserve_yookassa_order("order-1", 2, Decimal("120.00")))
    asyncio.run(db.attach_yookassa_payment("order-1", "provider-1"))
    old_end = asyncio.run(db.subscription_end(2))
    remote = YooKassaPayment("provider-1", "succeeded", None, Decimal("120.00"), "RUB", {"order_id": "order-1", "user_id": "2"})
    lookup = AsyncMock(return_value=remote); monkeypatch.setattr(YooKassaClient, "get_payment", lookup)
    payload = {"event": "payment.succeeded", "object": {"id": "provider-1", "status": "succeeded"}}
    assert client.post("/api/payment/webhook", json=payload).status_code == 403
    with TestClient(create_app(replace(settings, trust_proxy_headers=True), db)) as webhook_client:
        for _ in range(2):
            assert webhook_client.post("/api/payment/webhook", json=payload, headers={"X-Real-IP": "185.71.76.1"}).status_code == 200
    assert lookup.await_count == 2
    assert datetime.fromisoformat(asyncio.run(db.subscription_end(2))) == datetime.fromisoformat(old_end) + timedelta(days=30)
    assert client.get("/?payment=return").status_code == 200
    assert client.post("/api/yoomoney/notification").status_code == 404


def test_public_shell_contains_no_material_content(project):
    client, _, _ = project
    create_material(client)
    response = client.get("/")
    assert "Острое воспаление" not in response.text
    assert "frame-ancestors https://web.telegram.org" in response.headers["content-security-policy"]
    assert "x-frame-options" not in response.headers and "no-store" in response.headers["cache-control"]


def test_storage_rejects_public_path_and_traversal(tmp_path):
    with pytest.raises(ValueError):
        MediaStorage(str(Path(__file__).resolve().parents[1] / "web" / "uploads"))
    storage = MediaStorage(str(tmp_path / "media"))
    with pytest.raises(ValueError):
        storage.render("../secret.jpg", 1)
