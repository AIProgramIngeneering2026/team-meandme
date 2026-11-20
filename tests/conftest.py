from io import BytesIO

import pytest
from PIL import Image

from meandme.analysis import DuplicateFinder, QualityAnalyzer
from meandme.contracts import AnalysisInput
from meandme.dispatch import Dispatcher, Registry
from meandme.images import prepare_image


@pytest.fixture
def image_bytes():
    def make(color, *, mode="RGB", size=(4, 4), format="PNG"):
        with Image.new(mode, size, color) as image, BytesIO() as buffer:
            image.save(buffer, format=format)
            return buffer.getvalue()

    return make


@pytest.fixture
def request_with_duplicates(image_bytes):
    content = image_bytes((30, 30, 30), format="JPEG")
    return AnalysisInput((
        prepare_image("b", content),
        prepare_image("a", content),
        prepare_image("other", image_bytes((100, 100, 100))),
    ))


@pytest.fixture
def dispatcher():
    return Dispatcher(Registry({
        "find_duplicates": DuplicateFinder(),
        "assess_quality": QualityAnalyzer(),
    }))
