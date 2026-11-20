"""Два исполнителя для демонстрационного анализа фотогалереи."""

from collections import defaultdict
from hashlib import sha256

import numpy as np
from PIL import Image

from .contracts import AnalysisInput, ImageError, ImageInput, ToolResult
from .images import decoded_image, validate_analysis_input

BRIGHTNESS_THRESHOLD = 40
SHARPNESS_THRESHOLD = 100


class DuplicateFinder:
    def execute(self, request: AnalysisInput) -> ToolResult:
        try:
            validate_analysis_input(request)
        except ImageError as exc:
            return {"status": "error", "error": exc.error}

        by_hash: dict[str, list[str]] = defaultdict(list)
        for image in request.images:
            by_hash[sha256(image.content).hexdigest()].append(image.image_id)
        groups = [
            {"sha256": digest, "image_ids": sorted(image_ids)}
            for digest, image_ids in by_hash.items()
            if len(image_ids) > 1
        ]
        groups.sort(key=lambda group: group["image_ids"][0])
        return {
            "status": "ok",
            "operation": "find_duplicates",
            "data": {"image_count": len(request.images), "groups": groups},
        }


class QualityAnalyzer:
    def execute(self, request: AnalysisInput) -> ToolResult:
        try:
            validate_analysis_input(request)
            images = [_score(image) for image in sorted(request.images, key=lambda item: item.image_id)]
        except ImageError as exc:
            return {"status": "error", "error": exc.error}

        return {
            "status": "ok",
            "operation": "assess_quality",
            "data": {
                "image_count": len(images),
                "thresholds": {"brightness": BRIGHTNESS_THRESHOLD, "sharpness": SHARPNESS_THRESHOLD},
                "images": images,
            },
        }


def _score(source: ImageInput) -> dict[str, object]:
    field = f"images.{source.image_id}"
    with decoded_image(source.content, f"{field}.content") as image:
        if (image.format, *image.size) != (source.format, source.width, source.height):
            raise ImageError("invalid_tool_input", "Метаданные не совпадают с изображением", field)
        rgb = _rgb_on_white(image)

    # Храним Y в тысячных, чтобы не получить 39.999999… для серого 40.
    luminance = 299 * rgb[:, :, 0] + 587 * rgb[:, :, 1] + 114 * rgb[:, :, 2]
    center = luminance[1:-1, 1:-1]
    laplacian = (
        luminance[1:-1, :-2] + luminance[1:-1, 2:]
        + luminance[:-2, 1:-1] + luminance[2:, 1:-1] - 4 * center
    )
    brightness = float(luminance.mean() / 1000)
    sharpness = float(laplacian.var(ddof=0) / 1_000_000)
    warnings = []
    if brightness < BRIGHTNESS_THRESHOLD:
        warnings.append({"code": "low_brightness", "reason": "Средняя яркость ниже порога 40"})
    if sharpness < SHARPNESS_THRESHOLD:
        warnings.append({"code": "possible_blur", "reason": "Дисперсия Лапласа ниже порога 100"})
    return {
        "image_id": source.image_id, "brightness": brightness, "sharpness": sharpness, "warnings": warnings,
    }


def _rgb_on_white(image: Image.Image) -> np.ndarray:
    if image.format == "PNG" and ("A" in image.getbands() or "transparency" in image.info):
        with (
            image.convert("RGBA") as foreground,
            Image.new("RGBA", image.size, "white") as background,
            Image.alpha_composite(background, foreground) as composited,
            composited.convert("RGB") as rgb,
        ):
            return np.asarray(rgb, dtype=np.float64)
    with image.convert("RGB") as rgb:
        return np.asarray(rgb, dtype=np.float64)
