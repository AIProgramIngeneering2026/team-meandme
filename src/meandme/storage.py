"""SQLite-адаптер для JobRepository. Схемой управляет Alembic."""

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine, RowMapping, URL
from sqlalchemy.exc import SQLAlchemyError

from .catalog import CatalogItem, Dataset
from .job import JobSnapshot, JobState
from .repository import JobRecord, JobTransaction, StorageUnavailable

logger = logging.getLogger(__name__)

jobs = sa.table(
    "jobs",
    sa.column("id"),
    sa.column("idempotency_key"),
    sa.column("dataset_id"),
    sa.column("operation"),
    sa.column("state"),
    sa.column("items", sa.JSON),
    sa.column("catalog_version"),
    sa.column("analysis_version"),
    sa.column("correlation_id"),
    sa.column("created_at"),
    sa.column("started_at"),
    sa.column("finished_at"),
    sa.column("result", sa.JSON(none_as_null=True)),
    sa.column("error", sa.JSON(none_as_null=True)),
)


def sqlite_engine(path: str | None = None) -> Engine:
    database = path if path is not None else os.environ.get("MEANDME_DB", "meandme.sqlite3")
    return sa.create_engine(URL.create("sqlite+pysqlite", database=database), connect_args={"timeout": 5})


class SQLiteJobRepository:
    def __init__(self, engine: Engine):
        self.engine = engine

    @contextmanager
    def transaction(self, *, write: bool = False) -> Iterator[JobTransaction]:
        try:
            with self.engine.begin() as connection:
                if write:
                    # Блокируем запись до чтения ключа или состояния, в том числе в других соединениях.
                    connection.exec_driver_sql("BEGIN IMMEDIATE")
                yield _SQLiteTransaction(connection)
        except SQLAlchemyError as exc:
            logger.exception("Ошибка хранилища Job")
            raise StorageUnavailable("Хранилище недоступно") from exc

    def check(self) -> None:
        with self.transaction() as transaction:
            transaction.pending_count()


class _SQLiteTransaction:
    def __init__(self, connection: Connection):
        self._connection = connection

    def get(self, job_id: UUID) -> JobRecord | None:
        row = self._connection.execute(sa.select(jobs).where(jobs.c.id == str(job_id))).mappings().one_or_none()
        return _record(row) if row is not None else None

    def by_key(self, key: str) -> JobRecord | None:
        row = self._connection.execute(
            sa.select(jobs).where(jobs.c.idempotency_key == key)
        ).mappings().one_or_none()
        return _record(row) if row is not None else None

    def pending_count(self) -> int:
        return self._connection.execute(
            sa.select(sa.func.count()).select_from(jobs)
            .where(jobs.c.state.in_([JobState.CREATED, JobState.RUNNING]))
        ).scalar_one()

    def first_created(self) -> JobRecord | None:
        row = self._connection.execute(
            sa.select(jobs).where(jobs.c.state == JobState.CREATED)
            .order_by(jobs.c.created_at, jobs.c.id).limit(1)
        ).mappings().one_or_none()
        return _record(row) if row is not None else None

    def running(self) -> list[JobRecord]:
        rows = self._connection.execute(sa.select(jobs).where(jobs.c.state == JobState.RUNNING)).mappings()
        return [_record(row) for row in rows]

    def add(self, record: JobRecord) -> None:
        job = record.job
        self._connection.execute(
            jobs.insert().values(
                id=str(job.id),
                idempotency_key=record.key,
                dataset_id=job.dataset_id,
                operation=job.operation,
                state=job.state,
                items=[asdict(item) for item in record.dataset.items],
                catalog_version=record.dataset.version,
                analysis_version=record.analysis_version,
                correlation_id=str(record.correlation_id),
                created_at=job.created_at.isoformat(),
            )
        )

    def save(self, snapshot: JobSnapshot, previous: JobState) -> None:
        updated = self._connection.execute(
            jobs.update().where(jobs.c.id == str(snapshot.id), jobs.c.state == previous)
            .values(
                state=snapshot.state,
                started_at=_timestamp(snapshot.started_at),
                finished_at=_timestamp(snapshot.finished_at),
                result=snapshot.result,
                error=snapshot.error,
            )
        )
        if updated.rowcount != 1:
            raise StorageUnavailable("Сохранённое состояние Job изменилось")


def _timestamp(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_timestamp(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def _record(row: RowMapping) -> JobRecord:
    return JobRecord(
        JobSnapshot(
            id=UUID(row["id"]),
            operation=row["operation"],
            dataset_id=row["dataset_id"],
            state=JobState(row["state"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            started_at=_parse_timestamp(row["started_at"]),
            finished_at=_parse_timestamp(row["finished_at"]),
            result=row["result"],
            error=row["error"],
        ),
        row["idempotency_key"],
        Dataset(row["catalog_version"], tuple(CatalogItem(**item) for item in row["items"])),
        row["analysis_version"],
        UUID(row["correlation_id"]),
    )
