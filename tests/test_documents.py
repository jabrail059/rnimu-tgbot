import asyncio
import io
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.documents import DocumentStorage
from app.web import create_app
from test_library_api import project, auth, create_material


def pdf_bytes(page_count=3, padding=0):
    """Small real PDF with differently colored pages and optional large comments."""
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for index in range(page_count):
        page_id = len(objects) + 1
        kids.append(f"{page_id} 0 R")
        width, height = (600, 800) if index % 2 == 0 else (800, 600)
        stream = f"{index / max(page_count, 1)} 0.2 0.3 rg 20 20 100 80 re f BT /F1 20 Tf 30 150 Td (Page {index + 1}) Tj ET".encode()
        objects.extend([
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] /Resources << /Font << /F1 3 0 R >> >> /Contents {page_id + 1} 0 R >>".encode(),
            f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream",
        ])
    objects[1] = f"<< /Type /Pages /Count {page_count} /Kids [{' '.join(kids)}] >>".encode()
    result = bytearray(b"%PDF-1.4\n")
    if padding:
        result.extend(b"%" + b"x" * padding + b"\n")
    offsets = [0]
    for number, value in enumerate(objects, 1):
        offsets.append(len(result))
        result.extend(f"{number} 0 obj\n".encode() + value + b"\nendobj\n")
    start = len(result)
    result.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        result.extend(f"{offset:010d} 00000 n \n".encode())
    result.extend(f"trailer << /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode())
    return bytes(result)


def upload(client, material, data=None):
    return client.post(f"/api/admin/materials/{material}/documents", headers=auth(1),
                       params={"title": "Лекция.pdf"}, content=data if data is not None else pdf_bytes())


def test_pdf_pages_private_ordered_and_original_not_exposed(project):
    client, db, settings = project
    _, material = create_material(client)
    response = upload(client, material)
    assert response.status_code == 201 and response.json()["page_count"] == 3
    document_id = response.json()["id"]
    data = client.get(f"/api/materials/{material}", headers=auth(2)).json()
    metadata = data["documents"][0]
    assert metadata["title"] == "Лекция.pdf"
    assert metadata["page_sizes"] == [[600, 800], [800, 600], [600, 800]]
    assert "filename" not in metadata
    record = asyncio.run(db.document(document_id))
    assert (Path(settings.media_path) / record["filename"]).stat().st_mode & 0o777 == 0o600
    assert client.get(f"/static/{record['filename']}").status_code == 404
    assert client.get(f"/api/documents/{document_id}", headers=auth(2)).status_code == 404
    path = f"/api/documents/{document_id}/pages/1"
    assert client.get(path).status_code == 401
    assert client.get(path, headers=auth(3)).status_code == 403
    assert client.get(path, headers=auth(2, age=3601)).status_code == 401
    first = client.get(path, headers=auth(2))
    assert first.status_code == 200 and first.headers["content-type"] == "image/jpeg"
    assert "no-store" in first.headers["cache-control"] and first.headers["vary"] == "Authorization"
    decoded = Image.open(io.BytesIO(first.content))
    assert decoded.size == (1500, 2000)
    assert decoded.getpixel((750, 500)) == (255, 255, 255)
    assert first.content == client.get(path, headers=auth(1)).content
    second = client.get(f"/api/documents/{document_id}/pages/2", headers=auth(2))
    assert second.content != first.content
    assert Image.open(io.BytesIO(second.content)).size == (2000, 1500)
    for page in (0, 4, -1):
        assert client.get(f"/api/documents/{document_id}/pages/{page}", headers=auth(2)).status_code == 404
    with sqlite3.connect(settings.database_path) as connection:
        connection.execute("UPDATE users SET subscription_end=NULL WHERE user_id=2")
    assert client.get(path, headers=auth(2)).status_code == 403


@pytest.mark.parametrize("kind", ["documents", "materials", "categories"])
def test_pdf_deletion_and_cascade_remove_private_files(project, kind):
    client, db, settings = project
    category, material = create_material(client)
    document_id = upload(client, material).json()["id"]
    identifier = {"documents": document_id, "materials": material, "categories": category}[kind]
    assert client.delete(f"/api/admin/{kind}/{identifier}", headers=auth(1)).status_code == 200
    assert asyncio.run(db.document(document_id)) is None
    assert not list(Path(settings.media_path).iterdir())
    assert client.get(f"/api/documents/{document_id}/pages/1", headers=auth(1)).status_code == 404


def test_pdf_accepts_files_larger_than_photo_limit(project):
    client, _, settings = project
    _, material = create_material(client)
    assert settings.max_pdf_bytes == 512 * 1024 * 1024
    large = pdf_bytes(padding=settings.max_image_bytes + 1)
    response = upload(client, material, large)
    assert response.status_code == 201


def test_invalid_pdf_and_chunked_overflow_leave_no_files(project):
    client, db, settings = project
    _, material = create_material(client)
    for data in (b"", b"not a PDF", b"%PDF-1.7\nbroken", pdf_bytes(page_count=0)):
        assert upload(client, material, data).status_code == 422
    with TestClient(create_app(replace(settings, max_pdf_bytes=100), db)) as limited:
        assert upload(limited, material).status_code == 413
        assert upload(limited, material, iter([b"x" * 60, b"y" * 60])).status_code == 413
    assert not list(Path(settings.media_path).iterdir())
    assert asyncio.run(db.material(material))["documents"] == []
    with pytest.raises(ValueError):
        DocumentStorage(settings.media_path).render("../secret.pdf", 0)
