from dataclasses import replace
from io import BytesIO

import pytest
from PIL import Image

from meandme.analysis import DuplicateFinder, QualityAnalyzer
from meandme.contracts import AnalysisInput, ImageError
from meandme.images import prepare_image
from meandme.job import AnalysisJob, JobState


def test_duplicates_require_equal_bytes(image_bytes):
    color = (40, 40, 40)
    png = prepare_image("png", image_bytes(color))
    jpeg = prepare_image("jpeg", image_bytes(color, format="JPEG"))
    result = DuplicateFinder().execute(AnalysisInput((png, jpeg)))
    assert result["data"] == {"image_count": 2, "groups": []}


def test_multiple_duplicate_groups_are_sorted(image_bytes):
    first = prepare_image("b", image_bytes((0, 0, 0)))
    second = prepare_image("d", image_bytes((255, 255, 255)))
    request = AnalysisInput((second, first, replace(second, image_id="c"), replace(first, image_id="a")))
    result = DuplicateFinder().execute(request)
    assert [group["image_ids"] for group in result["data"]["groups"]] == [["a", "b"], ["c", "d"]]


def test_quality_reports_warnings_without_failing_job(dispatcher, image_bytes):
    dark = prepare_image("a-dark", image_bytes((20, 20, 20)))
    transparent = prepare_image("b-transparent", image_bytes((0, 0, 0, 0), mode="RGBA"))
    job = AnalysisJob("assess_quality", "demo-01")
    result = dispatcher.run(job, AnalysisInput((transparent, dark)))
    assert job.state == JobState.DONE
    assert result["data"]["thresholds"] == {"brightness": 40, "sharpness": 100}
    first, second = result["data"]["images"]
    assert first["image_id"] == "a-dark"
    assert first["brightness"] == 20
    assert first["sharpness"] == 0
    assert [w["code"] for w in first["warnings"]] == ["low_brightness", "possible_blur"]
    assert second["image_id"] == "b-transparent"
    assert second["brightness"] == 255
    assert [w["code"] for w in second["warnings"]] == ["possible_blur"]


def test_quality_thresholds_do_not_warn_at_equality(image_bytes):
    at_brightness = prepare_image("a", image_bytes((40, 40, 40)))
    with Image.new("RGB", (4, 3), (50, 50, 50)) as image, BytesIO() as buffer:
        image.putpixel((1, 0), (40, 40, 40))
        image.putpixel((2, 0), (60, 60, 60))
        image.save(buffer, format="PNG")
        at_sharpness = prepare_image("b", buffer.getvalue())
    result = QualityAnalyzer().execute(AnalysisInput((at_brightness, at_sharpness)))
    first, second = result["data"]["images"]
    assert first["brightness"] == 40
    assert [w["code"] for w in first["warnings"]] == ["possible_blur"]
    assert second["sharpness"] == 100
    assert second["warnings"] == []


def test_quality_uses_rgb_weights(image_bytes):
    red = prepare_image("red", image_bytes((255, 0, 0)))
    item = QualityAnalyzer().execute(AnalysisInput((red,)))["data"]["images"][0]
    assert item["brightness"] == pytest.approx(76.245)


@pytest.mark.parametrize("tool", [DuplicateFinder(), QualityAnalyzer()])
def test_both_tools_reject_repeated_image_ids(tool, image_bytes):
    image = prepare_image("same-id", image_bytes((0, 0, 0)))
    result = tool.execute(AnalysisInput((image, image)))
    assert result["error"]["code"] == "invalid_tool_input"
    assert result["error"]["details"][0]["field"] == "images.1.image_id"


def test_corrupt_image_returns_field_without_partial_report(image_bytes):
    first = prepare_image("a", image_bytes((0, 0, 0)))
    broken = replace(first, image_id="b", content=b"broken")
    result = QualityAnalyzer().execute(AnalysisInput((first, broken)))
    assert set(result) == {"status", "error"}
    assert result["error"]["code"] == "invalid_image"
    assert result["error"]["details"][0]["field"] == "images.b.content"


def test_quality_rejects_inconsistent_metadata(image_bytes):
    image = prepare_image("photo", image_bytes((0, 0, 0)))
    result = QualityAnalyzer().execute(AnalysisInput((replace(image, width=5),)))
    assert result["error"]["code"] == "invalid_tool_input"
    assert result["error"]["details"][0]["field"] == "images.photo"


@pytest.mark.parametrize("format,size,code", [
    ("GIF", (4, 4), "unsupported_image_format"),
    ("PNG", (2, 5), "invalid_image"),
])
def test_image_preparation_rejects_unsupported_or_small_files(image_bytes, format, size, code):
    with pytest.raises(ImageError) as exc:
        prepare_image("photo", image_bytes((0, 0, 0), size=size, format=format))
    assert exc.value.error["code"] == code


def test_image_preparation_rejects_large_dimensions_before_decoding():
    with Image.new("1", (4000, 3001)) as image, BytesIO() as buffer:
        image.save(buffer, format="PNG")
        with pytest.raises(ImageError) as exc:
            prepare_image("photo", buffer.getvalue())
    assert exc.value.error["code"] == "image_too_large"


def test_image_preparation_rejects_animated_png():
    with Image.new("RGBA", (4, 4), "red") as first, Image.new("RGBA", (4, 4), "blue") as second, BytesIO() as buffer:
        first.save(buffer, format="PNG", save_all=True, append_images=[second])
        with pytest.raises(ImageError) as exc:
            prepare_image("photo", buffer.getvalue())
    assert exc.value.error["code"] == "unsupported_image_format"
