"""Тонкий HTTP-слой контракта docs/api/openapi.yaml."""

import json
import logging
import re
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

from fastapi import FastAPI, Request
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from .analysis import DuplicateFinder, QualityAnalyzer
from .catalog import DEMO_CATALOG, Dataset, demo_image
from .dispatch import Dispatcher, Registry
from .repository import JobRecord, JobRepository, StorageUnavailable
from .runner import JobRunner
from .service import JobError, JobService
from .storage import SQLiteJobRepository, sqlite_engine

logger = logging.getLogger(__name__)

MAX_REQUEST_BYTES = 8192
KEY_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}")
DATASET_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")

ERROR_STATUS = {
    "invalid_json": 400,
    "route_not_found": 404,
    "job_not_found": 404,
    "method_not_allowed": 405,
    "idempotency_conflict": 409,
    "job_not_cancellable": 409,
    "result_not_ready": 409,
    "job_failed": 409,
    "job_cancelled": 409,
    "request_too_large": 413,
    "unsupported_media_type": 415,
    "invalid_request": 422,
    "unknown_operation": 422,
    "unknown_dataset": 422,
    "internal_error": 500,
    "storage_unavailable": 503,
    "catalog_unavailable": 503,
    "service_busy": 503,
    "service_unavailable": 503,
}


def _error_response(request: Request, error: JobError, headers=None) -> JSONResponse:
    correlation_id = str(request.state.correlation_id)
    return JSONResponse(
        {**error.error, "correlation_id": correlation_id},
        status_code=ERROR_STATUS[error.error["code"]],
        headers={**(headers or {}), "X-Correlation-ID": correlation_id},
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Повторяющееся имя поля")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError("В JSON нет таких числовых значений")


async def _create_parameters(request: Request) -> tuple[str, str, str]:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_REQUEST_BYTES:
            raise JobError("request_too_large", "Тело запроса больше 8192 байт", "body")
        body.extend(chunk)

    content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    encoding = request.headers.get("content-encoding", "identity").strip().lower()
    details = []
    if content_type != "application/json":
        details.append(("header.Content-Type", "Ожидается application/json"))
    if encoding != "identity":
        details.append(("header.Content-Encoding", "Сжатие тела не поддерживается"))
    if details:
        error = JobError("unsupported_media_type", "Неподдерживаемый формат тела")
        error.error["details"] = [{"field": field, "reason": reason} for field, reason in details]
        raise error

    try:
        payload = json.loads(
            body.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_invalid_constant
        )
    except (ValueError, RecursionError) as exc:
        raise JobError(
            "invalid_json", "Некорректный JSON", "body", "Проверьте синтаксис и уникальность имён полей"
        ) from exc

    keys = request.headers.getlist("idempotency-key")
    details = []
    if len(keys) != 1 or not KEY_PATTERN.fullmatch(keys[0]):
        details.append(("header.Idempotency-Key", "Нужен один ключ из 1–128 допустимых символов"))
    if isinstance(payload, dict):
        dataset_id = payload.get("dataset_id")
        operation = payload.get("operation")
        if not isinstance(dataset_id, str) or not DATASET_PATTERN.fullmatch(dataset_id):
            details.append(("body.dataset_id", "Нужен ID из 1–64 допустимых символов"))
        if not isinstance(operation, str) or not 1 <= len(operation) <= 64:
            details.append(("body.operation", "Нужна непустая строка длиной до 64 символов"))
        if payload.keys() - {"dataset_id", "operation"}:
            details.append(("body", "Допустимы только поля dataset_id и operation"))
    else:
        details.append(("body", "Ожидается JSON-объект"))
    if details:
        error = JobError("invalid_request", "Неверные параметры запроса")
        error.error["details"] = [{"field": field, "reason": reason} for field, reason in details]
        raise error
    return keys[0], dataset_id, operation


def _job_id(id: str) -> UUID:
    try:
        job_id = UUID(id)
        if str(job_id) == id.lower():
            return job_id
    except ValueError:
        pass
    raise JobError("invalid_request", "Неверный UUID задания", "path.id")


def _job_view(record: JobRecord) -> dict[str, object]:
    job = record.job
    return {
        "id": str(job.id),
        "dataset_id": job.dataset_id,
        "operation": job.operation,
        "state": job.state,
        "image_ids": [item.image_id for item in record.dataset.items],
        "catalog_version": record.dataset.version,
        "analysis_version": record.analysis_version,
        "created_at": job.created_at.isoformat(),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "correlation_id": str(record.correlation_id),
        "error": job.error,
    }


def create_app(
    *,
    repository: JobRepository | None = None,
    catalog: Mapping[str, Dataset] = DEMO_CATALOG,
    read_image: Callable[[str], bytes] = demo_image,
) -> FastAPI:
    engine = None
    if repository is None:
        engine = sqlite_engine()
        repository = SQLiteJobRepository(engine)
    registry = Registry({"find_duplicates": DuplicateFinder(), "assess_quality": QualityAnalyzer()})
    service = JobService(repository, registry, catalog)
    runner = JobRunner(repository, Dispatcher(registry), read_image)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            await run_in_threadpool(runner.start)
        except StorageUnavailable:
            logger.error("Обработчик не запущен. Примените миграции и перезапустите сервис")
        try:
            yield
        finally:
            await run_in_threadpool(runner.stop)
            if engine is not None:
                engine.dispose()

    app = FastAPI(lifespan=lifespan, openapi_url=None, redirect_slashes=False)

    @app.middleware("http")
    async def correlate(request: Request, call_next):
        request.state.correlation_id = uuid4()
        response = await call_next(request)
        response.headers["X-Correlation-ID"] = str(request.state.correlation_id)
        return response

    @app.exception_handler(JobError)
    async def job_error(request: Request, exc: JobError):
        return _error_response(request, exc)

    @app.exception_handler(StorageUnavailable)
    async def storage_error(request: Request, exc: StorageUnavailable):
        return _error_response(request, JobError("storage_unavailable", "Хранилище недоступно"))

    @app.exception_handler(HTTPException)
    async def routing_error(request: Request, exc: HTTPException):
        if exc.status_code == 404:
            error = JobError("route_not_found", "Маршрут не найден", "path", "Операция API не поддерживается")
        elif exc.status_code == 405:
            error = JobError(
                "method_not_allowed", "Метод не поддерживается", "method", "Используйте метод из Allow"
            )
        else:
            error = JobError("internal_error", "Внутренняя ошибка")
        return _error_response(request, error, exc.headers)

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        logger.error("Ошибка HTTP correlation_id=%s", request.state.correlation_id, exc_info=exc)
        return _error_response(request, JobError("internal_error", "Внутренняя ошибка"))

    @app.post("/jobs")
    async def create_job(request: Request):
        key, dataset_id, operation = await _create_parameters(request)
        record, created = await run_in_threadpool(
            service.create, key, dataset_id, operation, request.state.correlation_id
        )
        if created:
            return JSONResponse(_job_view(record), 202, {"Location": f"/jobs/{record.job.id}"})
        return _job_view(record)

    @app.get("/jobs/{id}")
    def get_job(id: str):
        return _job_view(service.get(_job_id(id)))

    @app.get("/jobs/{id}/result")
    def get_result(id: str):
        record, report = service.result(_job_id(id))
        return {
            "job_id": str(record.job.id),
            "operation": report["operation"],
            "analysis_version": record.analysis_version,
            "data": report["data"],
        }

    @app.post("/jobs/{id}/cancel")
    async def cancel_job(id: str, request: Request):
        job_id = _job_id(id)
        if "idempotency-key" in request.headers:
            raise JobError("invalid_request", "Отмена не принимает ключ", "header.Idempotency-Key")
        async for chunk in request.stream():
            if chunk:
                raise JobError("invalid_request", "Отмена вызывается без тела", "body")
        return _job_view(await run_in_threadpool(service.cancel, job_id))

    @app.get("/health")
    def get_health():
        repository.check()
        if not runner.ready:
            raise JobError("service_unavailable", "Обработчик заданий не готов")
        return {"status": "ok", "database": "ready", "runner": "ready"}

    return app
