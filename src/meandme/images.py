"""Проверка входа Tool и подготовка локальных JPEG/PNG."""

import re
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from io import BytesIO

from PIL import Image

from .contracts import (
    ANALYSIS_VERSION,
    IMAGE_FORMATS,
    MAX_FILE_BYTES,
    MAX_IMAGE_PIXELS,
    MAX_IMAGES,
    AnalysisInput,
    ImageError,
    ImageInput,
)

_IMAGE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _check_bytes(content: bytes, field: str) -> None:
    if type(content) is not bytes or not content:
        raise ImageError("invalid_tool_input", "Нужны непустые неизменяемые байты", field)
    if len(content) > MAX_FILE_BYTES:
        raise ImageError("image_too_large", "Файл больше 5 МиБ", field)


def _check_dimensions(width: int, height: int, field: str) -> None:
    if type(width) is not int or type(height) is not int:
        raise ImageError("invalid_tool_input", "Размеры должны быть целыми числами", field)
    if width < 3 or height < 3:
        raise ImageError("invalid_image", "Обе стороны изображения должны быть не меньше 3 пикселей", field)
    if width * height > MAX_IMAGE_PIXELS:
        raise ImageError("image_too_large", "В изображении больше 12 млн пикселей", field)


def validate_analysis_input(request: AnalysisInput) -> None:
    if not isinstance(request, AnalysisInput):
        raise ImageError("invalid_tool_input", "Ожидается AnalysisInput", "input")
    if request.analysis_version != ANALYSIS_VERSION:
        raise ImageError("invalid_tool_input", "Поддерживается версия анализа 1", "analysis_version")
    if not isinstance(request.images, tuple) or not 1 <= len(request.images) <= MAX_IMAGES:
        raise ImageError("invalid_tool_input", "Нужно от 1 до 10 изображений", "images")

    seen = set()
    for index, image in enumerate(request.images):
        field = f"images.{index}"
        if not isinstance(image, ImageInput):
            raise ImageError("invalid_tool_input", "Ожидается ImageInput", field)
        _check_id(image.image_id, f"{field}.image_id")
        if image.image_id in seen:
            raise ImageError("invalid_tool_input", "ID изображения повторяется", f"{field}.image_id")
        seen.add(image.image_id)
        field = f"images.{image.image_id}"
        _check_bytes(image.content, f"{field}.content")
        if image.format not in IMAGE_FORMATS:
            raise ImageError("unsupported_image_format", "Поддерживаются JPEG и PNG", f"{field}.format")
        _check_dimensions(image.width, image.height, field)


def _check_id(image_id: str, field: str) -> None:
    if not isinstance(image_id, str) or not _IMAGE_ID.fullmatch(image_id):
        raise ImageError("invalid_tool_input", "Неверный ID изображения", field)


@contextmanager
def decoded_image(content: bytes, field: str) -> Iterator[Image.Image]:
    """Проверить заголовок и размеры до загрузки пикселей."""
    with BytesIO(content) as source:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                image = Image.open(source)
        except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise ImageError("image_too_large", "Превышен предел размера изображения", field) from exc
        except (OSError, SyntaxError, ValueError) as exc:
            raise ImageError("invalid_image", "Не удалось открыть изображение", field) from exc

        with image:
            if image.format not in IMAGE_FORMATS or getattr(image, "n_frames", 1) != 1:
                raise ImageError("unsupported_image_format", "Нужен JPEG или PNG с одним кадром", field)
            _check_dimensions(*image.size, field)
            try:
                image.load()
            except (OSError, SyntaxError, ValueError) as exc:
                raise ImageError("invalid_image", "Не удалось декодировать изображение", field) from exc
            yield image


def prepare_image(image_id: str, content: bytes) -> ImageInput:
    """Проверка файла на стороне вызывающего перед передачей в Tool."""
    _check_id(image_id, "image_id")
    field = f"images.{image_id}.content"
    _check_bytes(content, field)
    with decoded_image(content, field) as image:
        width, height = image.size
        assert image.format is not None
        return ImageInput(image_id, content, image.format, width, height)
