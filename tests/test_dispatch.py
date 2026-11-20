import pytest

from meandme.contracts import AnalysisInput
from meandme.dispatch import Dispatcher, Registry, UnknownOperation
from meandme.job import AnalysisJob, JobState


def test_registry_rejects_unknown_operation():
    with pytest.raises(UnknownOperation) as exc:
        Registry({}).get("remove_photos")
    assert exc.value.error["code"] == "unknown_operation"
    assert exc.value.error["details"][0]["field"] == "operation"


def test_unknown_operation_in_existing_job_is_recorded(dispatcher):
    job = AnalysisJob("remove_photos", "demo-01")
    result = dispatcher.run(job, AnalysisInput(()))
    assert result["status"] == "error"
    assert result["error"]["code"] == "unknown_operation"
    assert job.state == JobState.FAILED


def test_invalid_parameters_report_field_and_fail_job(dispatcher):
    job = AnalysisJob("assess_quality", "demo-01")
    result = dispatcher.run(job, AnalysisInput((), analysis_version="2"))
    assert result["status"] == "error"
    assert result["error"]["code"] == "invalid_tool_input"
    assert result["error"]["details"][0]["field"] == "analysis_version"
    assert job.state == JobState.FAILED
    assert job.snapshot().error == result["error"]


def test_tool_exception_sets_safe_failure():
    class BrokenTool:
        def execute(self, request):
            raise RuntimeError("подробности для журнала")

    job = AnalysisJob("broken", "demo-01")
    result = Dispatcher(Registry({"broken": BrokenTool()})).run(job, AnalysisInput(()))
    assert result == {
        "status": "error",
        "error": {"code": "internal_error", "message": "Ошибка выполнения анализа", "details": []},
    }
    assert job.state == JobState.FAILED
    assert job.snapshot().result is None


def test_extending_registry_needs_no_dispatcher_changes():
    class ExtraTool:
        def execute(self, request):
            return {"status": "ok", "operation": "count_images", "data": {"total": len(request.images)}}

    dispatcher = Dispatcher(Registry({"count_images": ExtraTool()}))
    job = AnalysisJob("count_images", "demo-01")
    request = AnalysisInput(())
    assert dispatcher.execute(job.operation, request)["data"] == {"total": 0}
    assert job.state == JobState.CREATED
    assert dispatcher.run(job, request)["data"] == {"total": 0}
    assert job.state == JobState.DONE


def test_wrong_operation_in_tool_result_fails_job():
    class WrongTool:
        def execute(self, request):
            return {"status": "ok", "operation": "other", "data": {}}

    job = AnalysisJob("selected", "demo-01")
    result = Dispatcher(Registry({"selected": WrongTool()})).run(job, AnalysisInput(()))
    assert result["error"]["code"] == "internal_error"
    assert job.state == JobState.FAILED
