"""Вход, результат и ошибки общего контракта Tool."""

from dataclasses import dataclass
from typing import Literal, Protocol, TypedDict

ANALYSIS_VERSION = "1"
MAX_IMAGES = 10
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_PIXELS = 12_000_000
IMAGE_FORMATS = ("JPEG", "PNG")


@dataclass(frozen=True, slots=True)
class ImageInput:
    image_id: str
    content: bytes
    format: str
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class AnalysisInput:
    images: tuple[ImageInput, ...]
    analysis_version: str = ANALYSIS_VERSION


class ErrorDetail(TypedDict):
    field: str
    reason: str


class ToolError(TypedDict):
    code: str
    message: str
    details: list[ErrorDetail]


class ToolSuccess(TypedDict):
    status: Literal["ok"]
    operation: str
    data: dict[str, object]


class ToolFailure(TypedDict):
    status: Literal["error"]
    error: ToolError


type ToolResult = ToolSuccess | ToolFailure


class Tool(Protocol):
    def execute(self, request: AnalysisInput) -> ToolResult: ...


def tool_error(code: str, message: str, field: str | None = None) -> ToolError:
    details: list[ErrorDetail] = []
    if field is not None:
        details.append({"field": field, "reason": message})
    return {"code": code, "message": message, "details": details}


class ImageError(ValueError):
    """Отказ при подготовке или декодировании изображения."""

    def __init__(self, code: str, message: str, field: str):
        super().__init__(message)
        self.error = tool_error(code, message, field)
