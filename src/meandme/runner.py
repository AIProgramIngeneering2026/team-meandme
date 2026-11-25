"""Фоновый обработчик очереди одного процесса."""

import logging
from collections.abc import Callable
from threading import Event, Thread

from .catalog import load_dataset
from .contracts import ImageError, tool_error
from .dispatch import Dispatcher
from .job import AnalysisJob, JobState
from .repository import JobRepository, StorageUnavailable

logger = logging.getLogger(__name__)


class JobRunner:
    def __init__(
        self, repository: JobRepository, dispatcher: Dispatcher, read_image: Callable[[str], bytes]
    ):
        self.repository = repository
        self.dispatcher = dispatcher
        self.read_image = read_image
        self._stop = Event()
        self._thread: Thread | None = None

    @property
    def ready(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self._stop.is_set()

    def start(self) -> None:
        self.recover()
        self._stop.clear()
        self._thread = Thread(target=self._work, name="meandme-runner")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()

    def recover(self) -> None:
        with self.repository.transaction(write=True) as transaction:
            for record in transaction.running():
                job = AnalysisJob.restore(record.job)
                job.mark_interrupted()
                transaction.save(job.snapshot(), JobState.RUNNING)

    def run_next(self) -> bool:
        with self.repository.transaction() as transaction:
            if transaction.first_created() is None:
                return False
        with self.repository.transaction(write=True) as transaction:
            record = transaction.first_created()
            if record is None:
                return False
            job = AnalysisJob.restore(record.job)
            job.start()
            transaction.save(job.snapshot(), JobState.CREATED)

        try:
            request = load_dataset(record.dataset, record.analysis_version, self.read_image)
        except ImageError as exc:
            job.fail(exc.error)
        except Exception:
            logger.exception(
                "Сбой подготовки набора job_id=%s correlation_id=%s", record.job.id, record.correlation_id
            )
            job.fail(tool_error("internal_error", "Ошибка выполнения анализа"))
        else:
            result = self.dispatcher.execute(job.operation, request)
            if result["status"] == "ok":
                job.finish(result)
            else:
                job.fail(result["error"])

        with self.repository.transaction(write=True) as transaction:
            transaction.save(job.snapshot(), JobState.RUNNING)
        return True

    def _work(self) -> None:
        try:
            while not self._stop.is_set():
                if not self.run_next():
                    self._stop.wait(0.2)
        except StorageUnavailable:
            logger.error("Обработчик остановлен после сбоя записи или чтения БД")
        except Exception:
            logger.exception("Обработчик остановлен после непредвиденного сбоя")
