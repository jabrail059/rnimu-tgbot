from __future__ import annotations

import io
import re
import uuid
import warnings
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError


class InvalidImage(ValueError):
    pass


class MediaStorage:
    """Private, normalized photos. Original names and metadata are never retained."""

    def __init__(self, path: str):
        self.directory = Path(path).resolve()
        public_directory = Path(__file__).resolve().parent.parent / "web"
        if self.directory.is_relative_to(public_directory):
            raise ValueError("Media storage must be outside web/")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _path(self, filename: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}\.jpg", filename):
            raise ValueError("Invalid stored image name")
        path = self.directory / filename
        if path.is_symlink():
            raise ValueError("Image symlinks are not allowed")
        return path

    def save(self, data: bytes) -> str:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data), formats=("JPEG", "PNG", "WEBP")) as source:
                    if source.width * source.height > 20_000_000 or getattr(source, "n_frames", 1) != 1:
                        raise InvalidImage("Используйте неподвижное фото размером до 20 мегапикселей.")
                    source.load()
                    oriented = ImageOps.exif_transpose(source)
                    oriented.thumbnail((4096, 4096), Image.Resampling.LANCZOS)
                    # A fresh RGB image strips EXIF/GPS, comments and embedded profiles.
                    photo = Image.new("RGB", oriented.size, "white")
                    if "A" in oriented.getbands() or "transparency" in oriented.info:
                        rgba = oriented.convert("RGBA")
                        photo.paste(rgba, mask=rgba.getchannel("A"))
                    else:
                        photo.paste(oriented.convert("RGB"))
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            if isinstance(exc, InvalidImage):
                raise
            raise InvalidImage("Не удалось прочитать фото. Используйте JPEG, PNG или WebP.") from exc

        filename = f"{uuid.uuid4().hex}.jpg"
        path = self._path(filename)
        try:
            with path.open("xb") as target:
                photo.save(target, format="JPEG", quality=92)
            path.chmod(0o600)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return filename

    def render(self, filename: str, user_id: int) -> bytes:
        # Watermark is baked into the response, so removing a Canvas/DOM overlay
        # cannot recover a clean subscriber image.
        with Image.open(self._path(filename)) as source:
            photo = source.convert("RGBA")
        overlay = Image.new("RGBA", photo.size)
        draw = ImageDraw.Draw(overlay)
        font_size = max(10, min(42, photo.width // 32))
        font = ImageFont.load_default(size=font_size)
        label = f"@eucliris / ID {user_id}"
        text_width = draw.textbbox((0, 0), label, font=font)[2]
        step_x = max(text_width + font_size * 3, photo.width // 2)
        step_y = max(font_size * 7, photo.height // 4)
        for row, y in enumerate(range(font_size, photo.height, step_y)):
            for x in range(font_size - (step_x // 3 if row % 2 else 0), photo.width, step_x):
                draw.text((x, y), label, font=font, fill=(255, 255, 255, 75),
                          stroke_width=1, stroke_fill=(0, 0, 0, 55))
        result = io.BytesIO()
        Image.alpha_composite(photo, overlay).convert("RGB").save(result, format="JPEG", quality=92)
        return result.getvalue()

    def delete(self, filename: str) -> None:
        self._path(filename).unlink(missing_ok=True)
