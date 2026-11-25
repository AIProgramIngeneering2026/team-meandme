"""Демонстрационные наборы и подготовка входа для инструментов."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageDraw

from .contracts import MAX_IMAGES, AnalysisInput
from .images import prepare_image

_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,64}")


@dataclass(frozen=True, slots=True)
class CatalogItem:
    image_id: str
    asset: str | None = None
    copy_of: str | None = None


@dataclass(frozen=True, slots=True)
class Dataset:
    version: str
    items: tuple[CatalogItem, ...]

    def validate(self) -> None:
        if not self.version or not 1 <= len(self.items) <= MAX_IMAGES:
            raise ValueError("Неверная версия или число элементов набора")
        seen = set()
        originals = set()
        for item in self.items:
            if not _IDENTIFIER.fullmatch(item.image_id) or item.image_id in seen:
                raise ValueError("Неверный или повторяющийся ID изображения")
            if item.asset and item.copy_of is None:
                originals.add(item.image_id)
            elif item.asset is not None or item.copy_of not in originals:
                raise ValueError("Копия должна ссылаться на предшествующий оригинал")
            seen.add(item.image_id)


DEMO_CATALOG = {
    "demo-01": Dataset(
        "local-1",
        (
            CatalogItem("photo-10", asset="local-1/10"),
            CatalogItem("photo-29", asset="local-1/29"),
            CatalogItem("photo-10-copy", copy_of="photo-10"),
        ),
    )
}


def demo_image(asset: str) -> bytes:
    """Возвращает воспроизводимый JPEG для указанного локального ресурса."""
    background, foreground = {
        "local-1/10": ("#ddeaf4", "#1a4364"),
        "local-1/29": ("#10121a", "#22242c"),
    }[asset]
    with Image.new("RGB", (800, 600), background) as image, BytesIO() as data:
        ImageDraw.Draw(image).rectangle((128, 128, 672, 472), fill=foreground)
        image.save(data, format="JPEG")
        return data.getvalue()


def load_dataset(
    dataset: Dataset, analysis_version: str, read_image: Callable[[str], bytes]
) -> AnalysisInput:
    contents = {}
    images = []
    for item in dataset.items:
        if item.copy_of is None:
            assert item.asset is not None
            contents[item.image_id] = read_image(item.asset)
            content = contents[item.image_id]
        else:
            content = contents[item.copy_of]
        images.append(prepare_image(item.image_id, content))
    return AnalysisInput(tuple(images), analysis_version)
