from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from threading import Barrier, Event

import pytest

from meandme.contracts import AnalysisInput, tool_error
from meandme.dispatch import Dispatcher, Registry
from meandme.job import AnalysisJob, InvalidTransition, JobState


class BlockingTool:
    def __init__(self):
        self.entered = Event()
        self.release = Event()
        self.calls = 0

    def execute(self, request):
        self.calls += 1
        self.entered.set()
        assert self.release.wait(timeout=5)
        return {"status": "ok", "operation": "test_tool", "data": {}}


def successful_result():
    return {"status": "ok", "operation": "find_duplicates", "data": {"image_count": 1, "groups": []}}


def test_job_from_creation_to_success(dispatcher, request_with_duplicates):
    job = AnalysisJob("find_duplicates", "demo-01")
    initial = job.snapshot()
    assert initial.state == JobState.CREATED
    assert initial.result is initial.error is None

    result = dispatcher.run(job, request_with_duplicates)

    final = job.snapshot()
    assert final.id == initial.id
    assert final.state == JobState.DONE
    assert final.created_at <= final.started_at <= final.finished_at
    assert final.error is None
    assert final.result == result == {
        "status": "ok",
        "operation": "find_duplicates",
        "data": {
            "image_count": 3,
            "groups": [{
                "image_ids": ["a", "b"],
                "sha256": sha256(request_with_duplicates.images[0].content).hexdigest(),
            }],
        },
    }


def test_z1_two_starts_call_only_one_tool():
    job = AnalysisJob("test_tool", "demo-01")
    tool = BlockingTool()
    dispatcher = Dispatcher(Registry({"test_tool": tool}))
    request = AnalysisInput(())
    with ThreadPoolExecutor(max_workers=2) as workers:
        active = workers.submit(dispatcher.run, job, request)
        try:
            assert tool.entered.wait(timeout=5)
            other = workers.submit(dispatcher.run, job, request)
            with pytest.raises(InvalidTransition) as exc:
                other.result(timeout=5)
            assert exc.value.details == {"from": "running", "to": "running"}
            assert job.state == JobState.RUNNING
            assert tool.calls == 1
        finally:
            tool.release.set()
        assert active.result(timeout=5)["status"] == "ok"
    assert job.state == JobState.DONE


def test_z2_recovery_ends_interrupted_job():
    class StoppedTool:
        def execute(self, request):
            raise SystemExit("Остановка процесса")

    job = AnalysisJob("find_duplicates", "demo-01")
    dispatcher = Dispatcher(Registry({"find_duplicates": StoppedTool()}))
    with pytest.raises(SystemExit):
        dispatcher.run(job, AnalysisInput(()))

    assert job.state == JobState.RUNNING
    job.mark_interrupted()
    failed = job.snapshot()
    assert failed.state == JobState.FAILED
    assert failed.error["code"] == "execution_interrupted"
    assert failed.result is None
    with pytest.raises(InvalidTransition):
        job.finish(successful_result())
    assert job.snapshot() == failed


def test_z3_cancelled_job_cannot_finish_or_start(dispatcher, request_with_duplicates):
    job = AnalysisJob("find_duplicates", "demo-01")
    job.cancel()
    cancelled = job.snapshot()
    assert cancelled.state == JobState.CANCELLED
    assert cancelled.finished_at >= cancelled.created_at
    assert cancelled.started_at is cancelled.result is cancelled.error is None
    with pytest.raises(InvalidTransition):
        job.finish(successful_result())
    with pytest.raises(InvalidTransition):
        dispatcher.run(job, request_with_duplicates)
    assert job.snapshot() == cancelled


def test_z4_failed_job_rejects_late_success():
    job = AnalysisJob("find_duplicates", "demo-01")
    job.start()
    job.fail(tool_error("invalid_image", "Файл повреждён"))
    failed = job.snapshot()
    with pytest.raises(InvalidTransition) as exc:
        job.finish(successful_result())
    assert exc.value.details == {"from": "failed", "to": "done"}
    assert job.snapshot() == failed


def test_z5_success_before_running_is_rejected():
    job = AnalysisJob("find_duplicates", "demo-01")
    initial = job.snapshot()
    with pytest.raises(InvalidTransition) as exc:
        job.finish(successful_result())
    assert exc.value.code == "invalid_transition"
    assert exc.value.details == {"from": "created", "to": "done"}
    assert job.snapshot() == initial


def test_z6_completed_job_cannot_run_again(dispatcher, request_with_duplicates):
    job = AnalysisJob("find_duplicates", "demo-01")
    dispatcher.run(job, request_with_duplicates)
    done = job.snapshot()
    with pytest.raises(InvalidTransition) as exc:
        dispatcher.run(job, request_with_duplicates)
    assert exc.value.details == {"from": "done", "to": "running"}
    assert job.snapshot() == done


def test_cancellation_and_start_cannot_both_win():
    job = AnalysisJob("find_duplicates", "demo-01")
    barrier = Barrier(2, timeout=5)

    def attempt(action):
        barrier.wait()
        try:
            action()
        except InvalidTransition:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as workers:
        start = workers.submit(attempt, job.start)
        cancel = workers.submit(attempt, job.cancel)
        started, cancelled = start.result(timeout=5), cancel.result(timeout=5)

    assert started != cancelled
    assert job.state == (JobState.RUNNING if started else JobState.CANCELLED)


def test_saved_result_is_independent_of_returned_objects(dispatcher, request_with_duplicates):
    job = AnalysisJob("find_duplicates", "demo-01")
    result = dispatcher.run(job, request_with_duplicates)
    result["data"]["groups"].clear()
    snapshot = job.snapshot()
    snapshot.result["data"]["groups"].clear()
    assert len(job.snapshot().result["data"]["groups"]) == 1
