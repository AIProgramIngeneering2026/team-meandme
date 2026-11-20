"""Выбор исполнителя и выполнение Job над подготовленными изображениями."""

import logging
from collections.abc import Mapping

from .contracts import AnalysisInput, Tool, ToolResult, tool_error
from .job import AnalysisJob

logger = logging.getLogger(__name__)


class UnknownOperation(LookupError):
    def __init__(self, operation: str):
        super().__init__(f"Неизвестная операция: {operation}")
        self.error = tool_error("unknown_operation", f"Неизвестная операция: {operation}", "operation")


class Registry:
    def __init__(self, tools: Mapping[str, Tool]):
        self._tools = dict(tools)

    def get(self, operation: str) -> Tool:
        try:
            return self._tools[operation]
        except KeyError:
            raise UnknownOperation(operation) from None


class Dispatcher:
    def __init__(self, registry: Registry):
        self._registry = registry

    def execute(self, operation: str, request: AnalysisInput) -> ToolResult:
        """Вызвать Tool по контракту p2, отдельно от управления Job."""
        try:
            tool = self._registry.get(operation)
        except UnknownOperation as exc:
            return {"status": "error", "error": exc.error}

        try:
            result = tool.execute(request)
            if result["status"] == "ok":
                if result["operation"] == operation and isinstance(result["data"], dict):
                    return result
            elif result["status"] == "error":
                error = result["error"]
                if (
                    isinstance(error["code"], str)
                    and isinstance(error["message"], str)
                    and isinstance(error["details"], list)
                ):
                    return result
            logger.error("Tool вернул ответ вне контракта для %s", operation)
        except Exception:
            logger.exception("Сбой при выполнении %s", operation)
        return {"status": "error", "error": tool_error("internal_error", "Ошибка выполнения анализа")}

    def run(self, job: AnalysisJob, request: AnalysisInput) -> ToolResult:
        """Захватить Job, вызвать Tool и записать ровно один итог."""
        job.start()
        result = self.execute(job.operation, request)
        if result["status"] == "ok":
            job.finish(result)
        else:
            job.fail(result["error"])
        return result
