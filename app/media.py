from __future__ import annotations

import io
import re
import uuid
import warnings
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError


class InvalidImage(ValueError):
    pass


class MediaStorage:
    """Private, normalized photos. Original names and metadata are never retained."""

    def __init__(self, path: str):
        project_root = Path(__file__).resolve().parent.parent
        raw = Path(path).expanduser()
        self.directory = (project_root / raw if not raw.is_absolute() else raw).resolve()
        public_directory = project_root / "web"
        if self.directory.is_relative_to(public_directory):
            raise ValueError("Media storage must be outside web/")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)

    def _path(self, filename: str) -> Path:
        # Earlier releases stored original PNG/WebP files with UUID names.
        if not re.fullmatch(r"[0-9a-f]{32}\.(jpg|png|webp)", filename):
            raise ValueError("Invalid stored image name")
        path = self.directory / filename
        if path.is_symlink():
            raise ValueError("Image symlinks are not allowed")
        return path

    @staticmethod
    def _decode(source_file: io.BytesIO | Path) -> Image.Image:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(source_file, formats=("JPEG", "PNG", "WEBP")) as source:
                    if source.width * source.height > 50_000_000 or getattr(source, "n_frames", 1) != 1:
                        raise InvalidImage("Используйте неподвижное фото размером до 50 мегапикселей.")
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
        except FileNotFoundError:
            raise
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            if isinstance(exc, InvalidImage):
                raise
            raise InvalidImage("Не удалось прочитать фото. Используйте JPEG, PNG или WebP.") from exc
        return photo

    def save(self, data: bytes) -> str:
        photo = self._decode(io.BytesIO(data))
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

    def render(self, filename: str) -> bytes:
        photo = self._decode(self._path(filename))
        result = io.BytesIO()
        photo.save(result, format="JPEG", quality=92)
        return result.getvalue()

    def delete(self, filename: str) -> None:
        self._path(filename).unlink(missing_ok=True)
