"""Интерфейс хранения Job и реализация для модульных тестов."""

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from copy import deepcopy
from dataclasses import dataclass
from threading import Lock
from typing import Protocol
from uuid import UUID

from .catalog import Dataset
from .job import JobSnapshot, JobState


@dataclass(frozen=True, slots=True)
class JobRecord:
    job: JobSnapshot
    key: str
    dataset: Dataset
    analysis_version: str
    correlation_id: UUID


class StorageUnavailable(RuntimeError):
    pass


class JobTransaction(Protocol):
    def get(self, job_id: UUID) -> JobRecord | None: ...
    def by_key(self, key: str) -> JobRecord | None: ...
    def pending_count(self) -> int: ...
    def first_created(self) -> JobRecord | None: ...
    def running(self) -> list[JobRecord]: ...
    def add(self, record: JobRecord) -> None: ...
    def save(self, snapshot: JobSnapshot, previous: JobState) -> None: ...


class JobRepository(Protocol):
    def transaction(self, *, write: bool = False) -> AbstractContextManager[JobTransaction]: ...
    def check(self) -> None: ...


class MemoryJobRepository:
    def __init__(self) -> None:
        self._records: dict[UUID, JobRecord] = {}
        self._lock = Lock()

    @contextmanager
    def transaction(self, *, write: bool = False) -> Iterator[JobTransaction]:
        with self._lock:
            transaction = _MemoryTransaction(deepcopy(self._records))
            yield transaction
            if write:
                self._records = transaction.records

    def check(self) -> None:
        pass


class _MemoryTransaction:
    def __init__(self, records: dict[UUID, JobRecord]):
        self.records = records

    def get(self, job_id: UUID) -> JobRecord | None:
        return self.records.get(job_id)

    def by_key(self, key: str) -> JobRecord | None:
        return next((record for record in self.records.values() if record.key == key), None)

    def pending_count(self) -> int:
        return sum(
            record.job.state in (JobState.CREATED, JobState.RUNNING)
            for record in self.records.values()
        )

    def first_created(self) -> JobRecord | None:
        created = (record for record in self.records.values() if record.job.state == JobState.CREATED)
        return min(created, default=None, key=lambda record: (record.job.created_at, record.job.id))

    def running(self) -> list[JobRecord]:
        return [record for record in self.records.values() if record.job.state == JobState.RUNNING]

    def add(self, record: JobRecord) -> None:
        if record.job.id in self.records or self.by_key(record.key) is not None:
            raise StorageUnavailable("Повтор ID или ключа")
        self.records[record.job.id] = deepcopy(record)

    def save(self, snapshot: JobSnapshot, previous: JobState) -> None:
        record = self.records.get(snapshot.id)
        if record is None or record.job.state != previous:
            raise StorageUnavailable("Сохранённое состояние Job изменилось")
        self.records[snapshot.id] = JobRecord(
            deepcopy(snapshot), record.key, record.dataset, record.analysis_version, record.correlation_id
        )
