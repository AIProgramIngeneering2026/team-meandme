"""Приём заданий и чтение сохранённых итогов."""

from collections.abc import Mapping
from uuid import UUID

from .catalog import Dataset
from .contracts import ANALYSIS_VERSION, ToolSuccess, tool_error
from .dispatch import Registry, UnknownOperation
from .job import AnalysisJob, InvalidTransition, JobState
from .repository import JobRecord, JobRepository

MAX_PENDING_JOBS = 20


class JobError(ValueError):
    def __init__(self, code: str, message: str, field: str | None = None, reason: str | None = None):
        super().__init__(message)
        self.error = tool_error(code, message, field)
        if field is not None and reason is not None:
            self.error["details"][0]["reason"] = reason


def _missing_job() -> JobError:
    return JobError("job_not_found", "Задание не найдено", "path.id", "Job с этим UUID отсутствует")


class JobService:
    def __init__(self, repository: JobRepository, registry: Registry, catalog: Mapping[str, Dataset]):
        self.repository = repository
        self.registry = registry
        self.catalog = catalog

    def create(
        self, key: str, dataset_id: str, operation: str, correlation_id: UUID
    ) -> tuple[JobRecord, bool]:
        with self.repository.transaction(write=True) as transaction:
            previous = transaction.by_key(key)
            if previous is not None:
                if (previous.job.dataset_id, previous.job.operation) != (dataset_id, operation):
                    raise JobError(
                        "idempotency_conflict", "Ключ уже использован для других параметров",
                        "header.Idempotency-Key",
                    )
                return previous, False

            try:
                self.registry.get(operation)
            except UnknownOperation as exc:
                raise JobError("unknown_operation", "Операция не поддерживается", "body.operation") from exc
            dataset = self.catalog.get(dataset_id)
            if dataset is None:
                raise JobError("unknown_dataset", "Набор не найден в каталоге", "body.dataset_id")
            try:
                dataset.validate()
            except ValueError as exc:
                raise JobError("catalog_unavailable", "Набор временно недоступен") from exc
            if transaction.pending_count() >= MAX_PENDING_JOBS:
                raise JobError("service_busy", "Очередь заданий заполнена")

            record = JobRecord(
                AnalysisJob(operation, dataset_id).snapshot(), key, dataset, ANALYSIS_VERSION, correlation_id
            )
            transaction.add(record)
            return record, True

    def get(self, job_id: UUID) -> JobRecord:
        with self.repository.transaction() as transaction:
            record = transaction.get(job_id)
            if record is None:
                raise _missing_job()
            return record

    def result(self, job_id: UUID) -> tuple[JobRecord, ToolSuccess]:
        record = self.get(job_id)
        job = record.job
        if job.result is not None:
            return record, job.result
        if job.state == JobState.FAILED:
            assert job.error is not None
            raise JobError("job_failed", "Анализ завершился ошибкой", "job.error.code", job.error["code"])
        if job.state == JobState.CANCELLED:
            raise JobError("job_cancelled", "Задание отменено", "job.state", job.state.value)
        raise JobError("result_not_ready", "Результат ещё не готов", "job.state", job.state.value)

    def cancel(self, job_id: UUID) -> JobRecord:
        with self.repository.transaction(write=True) as transaction:
            record = transaction.get(job_id)
            if record is None:
                raise _missing_job()
            if record.job.state == JobState.CANCELLED:
                return record
            job = AnalysisJob.restore(record.job)
            try:
                job.cancel()
            except InvalidTransition as exc:
                raise JobError(
                    "job_not_cancellable", "Отмена доступна только до начала обработки",
                    "job.state", record.job.state.value,
                ) from exc
            snapshot = job.snapshot()
            transaction.save(snapshot, record.job.state)
            return JobRecord(snapshot, record.key, record.dataset, record.analysis_version, record.correlation_id)
