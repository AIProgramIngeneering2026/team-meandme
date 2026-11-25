"""Жизненный цикл задания и правила переходов."""

from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from threading import Lock
from uuid import UUID, uuid4

from .contracts import ToolError, ToolSuccess, tool_error


class JobState(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


_TRANSITIONS = {
    JobState.CREATED: frozenset({JobState.RUNNING, JobState.CANCELLED}),
    JobState.RUNNING: frozenset({JobState.DONE, JobState.FAILED}),
    JobState.DONE: frozenset(),
    JobState.FAILED: frozenset(),
    JobState.CANCELLED: frozenset(),
}


class InvalidTransition(ValueError):
    code = "invalid_transition"

    def __init__(self, current: JobState, target: JobState):
        self.details = {"from": current.value, "to": target.value}
        super().__init__(f"Переход из {current} в {target} запрещён")


@dataclass(frozen=True, slots=True)
class JobSnapshot:
    id: UUID
    operation: str
    dataset_id: str
    state: JobState
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: ToolSuccess | None = None
    error: ToolError | None = None


class AnalysisJob:
    def __init__(self, operation: str, dataset_id: str):
        self._lock = Lock()
        self._snapshot = JobSnapshot(
            id=uuid4(),
            operation=operation,
            dataset_id=dataset_id,
            state=JobState.CREATED,
            created_at=datetime.now(UTC),
        )

    @classmethod
    def restore(cls, snapshot: JobSnapshot) -> "AnalysisJob":
        job = cls.__new__(cls)
        job._lock = Lock()
        job._snapshot = deepcopy(snapshot)
        return job

    @property
    def operation(self) -> str:
        return self._snapshot.operation

    @property
    def state(self) -> JobState:
        with self._lock:
            return self._snapshot.state

    def snapshot(self) -> JobSnapshot:
        with self._lock:
            return deepcopy(self._snapshot)

    def start(self) -> None:
        self._transition(JobState.RUNNING)

    def cancel(self) -> None:
        self._transition(JobState.CANCELLED)

    def finish(self, result: ToolSuccess) -> None:
        self._transition(JobState.DONE, result=result)

    def fail(self, error: ToolError) -> None:
        self._transition(JobState.FAILED, error=error)

    def mark_interrupted(self) -> None:
        """Завершить ранее запущенный Job после остановки обработчика."""
        self.fail(tool_error("execution_interrupted", "Выполнение прервано при перезапуске"))

    def _transition(
        self,
        target: JobState,
        *,
        result: ToolSuccess | None = None,
        error: ToolError | None = None,
    ) -> None:
        with self._lock:
            current = self._snapshot
            if target not in _TRANSITIONS[current.state]:
                raise InvalidTransition(current.state, target)
            if target == JobState.DONE and (
                result is None
                or result["status"] != "ok"
                or result["operation"] != current.operation
                or not isinstance(result["data"], dict)
            ):
                raise ValueError("Для завершения нужен результат выбранной операции")
            if target == JobState.FAILED and error is None:
                raise ValueError("Для неуспешного завершения нужна причина ошибки")

            now = datetime.now(UTC)
            self._snapshot = replace(
                current,
                state=target,
                started_at=now if target == JobState.RUNNING else current.started_at,
                finished_at=None if target == JobState.RUNNING else now,
                result=deepcopy(result),
                error=deepcopy(error),
            )
