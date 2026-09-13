from __future__ import annotations

import io
import math
import os
import re
import threading
import uuid
from contextlib import closing
from pathlib import Path

import pypdfium2 as pdfium

from app.media import MediaStorage


class InvalidPDF(ValueError):
    pass


# PDFium must never run concurrently in different threads, even on different
# documents. All native handles are opened and closed while holding this lock.
# https://pypdfium2.readthedocs.io/en/stable/python_api.html#incompatibility-with-threading
_pdfium_lock = threading.Lock()


class DocumentStorage(MediaStorage):
    """Private originals; the API exposes only rasterized pages, never the PDF."""

    def _path(self, filename: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}\.pdf", filename):
            raise ValueError("Invalid stored PDF name")
        path = self.directory / filename
        if path.is_symlink():
            raise ValueError("PDF symlinks are not allowed")
        return path

    def create_upload(self):
        filename = f"{uuid.uuid4().hex}.pdf"
        descriptor = os.open(self._path(filename), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        return filename, os.fdopen(descriptor, "wb")

    @staticmethod
    def _size(size: tuple[float, float]) -> tuple[float, float]:
        if not all(math.isfinite(value) and value > 0 for value in size):
            raise InvalidPDF("В PDF обнаружена страница с некорректными размерами.")
        return size

    def inspect(self, filename: str) -> list[tuple[float, float]]:
        path = self._path(filename)
        with path.open("rb") as source:
            if b"%PDF-" not in source.read(1024):
                raise InvalidPDF("Выберите PDF-файл.")
        try:
            with _pdfium_lock, pdfium.PdfDocument(path) as document:
                if not len(document):
                    raise InvalidPDF("В PDF нет страниц.")
                return [self._size(document.get_page_size(index)) for index in range(len(document))]
        except (pdfium.PdfiumError, OSError, RuntimeError, ValueError) as exc:
            raise InvalidPDF("Не удалось прочитать PDF. Проверьте файл и снимите пароль, если он установлен.") from exc

    def render(self, filename: str, page_index: int) -> bytes:
        try:
            with _pdfium_lock, pdfium.PdfDocument(self._path(filename)) as document:
                if page_index < 0 or page_index >= len(document):
                    raise IndexError("PDF page not found")
                with closing(document.get_page(page_index)) as page:
                    width, height = self._size(page.get_size())
                    # Bound bitmap memory independently of PDF page dimensions.
                    scale = min(2.5, 2200 / width, 3200 / height, math.sqrt(6_000_000 / (width * height)))
                    with closing(page.render(scale=scale)) as bitmap, bitmap.to_pil() as source:
                        photo = source.convert("RGB")
                        try:
                            result = io.BytesIO()
                            photo.save(result, format="JPEG", quality=90)
                            return result.getvalue()
                        finally:
                            photo.close()
        except (pdfium.PdfiumError, OSError, RuntimeError, ValueError) as exc:
            raise InvalidPDF("Не удалось отобразить страницу PDF.") from exc
