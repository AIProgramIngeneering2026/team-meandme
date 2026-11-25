"""Проверки HTTP через приложение и SQLite, без сетевого источника изображений."""

import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event
from time import monotonic, sleep
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
import yaml
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker
from openapi_spec_validator import validate

from meandme.api import create_app
from meandme.catalog import DEMO_CATALOG, demo_image
from meandme.repository import MemoryJobRepository
from meandme.storage import SQLiteJobRepository, sqlite_engine

ROOT = Path(__file__).resolve().parents[1]
SPEC = yaml.safe_load((ROOT / "docs/api/openapi.yaml").read_text(encoding="utf-8"))
INPUT = {"dataset_id": "demo-01", "operation": "find_duplicates"}
KEY = {"Idempotency-Key": "practice-4"}


class ContractClient(TestClient):
    def request(self, method, url, **kwargs):
        response = super().request(method, url, **kwargs)
        path = response.request.url.path
        route = next(
            (
                definition for template, definition in SPEC["paths"].items()
                if re.fullmatch(template.replace("{id}", "[^/]+"), path)
            ),
            {},
        )
        operation = route.get(method.lower())
        if operation is not None:
            definition = operation["responses"][str(response.status_code)]
        else:
            name = "MethodNotAllowed" if route else "RouteNotFound"
            assert response.status_code == (405 if route else 404)
            definition = SPEC["components"]["responses"][name]

        assert response.headers["content-type"] == "application/json"
        for name, header in definition["headers"].items():
            Draft202012Validator(header["schema"], format_checker=FormatChecker()).validate(
                response.headers[name]
            )
        schema = definition["content"]["application/json"]["schema"]
        payload = response.json()
        Draft202012Validator(
            {**schema, "components": SPEC["components"]}, format_checker=FormatChecker()
        ).validate(payload)
        if response.status_code >= 400:
            assert payload["correlation_id"] == response.headers["X-Correlation-ID"]
        return response


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = tmp_path / "jobs.sqlite3"
    monkeypatch.setenv("MEANDME_DB", str(path))
    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
    return path


@pytest.fixture
def client(database):
    with ContractClient(create_app()) as http:
        yield http


def submit(client, *, key=KEY, body=INPUT):
    return client.post("/jobs", json=body, headers=key)


def job_state(client, path, state):
    deadline = monotonic() + 5
    while monotonic() < deadline:
        job = client.get(path).json()
        if job["state"] == state:
            return job
        if job["state"] in {"done", "cancelled", "failed"}:
            pytest.fail(f"Ожидался {state}, получен {job}")
        sleep(0.02)
    pytest.fail(f"Job не перешёл в {state}: {job}")


def test_openapi_paths_match_application():
    validate(SPEC)
    actual = {(route.path, method.lower())
              for route in create_app().routes for method in route.methods}
    expected = {(path, method) for path, operations in SPEC["paths"].items()
                for method in operations if method != "parameters"}
    assert actual == expected


@pytest.mark.parametrize("operation", ["find_duplicates", "assess_quality"])
def test_create_read_and_persist_result(database, operation):
    payload = {**INPUT, "operation": operation}
    with ContractClient(create_app()) as http:
        created = submit(http, body=payload)
        assert created.status_code == 202
        job = created.json()
        assert job["state"] == "created"
        assert job["correlation_id"] == created.headers["X-Correlation-ID"]
        path = created.headers["Location"]
        assert path == f"/jobs/{job['id']}"
        completed = job_state(http, path, "done")
        report = http.get(f"{path}/result").json()
        assert report["job_id"] == job["id"] and report["data"]["image_count"] == 3
        if operation == "find_duplicates":
            assert report["data"]["groups"][0]["image_ids"] == ["photo-10", "photo-10-copy"]
        else:
            assert len(report["data"]["images"]) == 3
        assert http.get("/health").status_code == 200

    def unexpected_read(asset):
        pytest.fail(f"Сохранённый результат вызвал чтение {asset}")

    with ContractClient(create_app(catalog={}, read_image=unexpected_read)) as restarted:
        assert restarted.get(path).json() == completed
        assert restarted.get(f"{path}/result").json() == report
        repeated = submit(restarted, body=payload)
        assert repeated.status_code == 200
        assert repeated.json() == completed
        assert repeated.headers["X-Correlation-ID"] != job["correlation_id"]


@pytest.mark.parametrize(
    ("payload", "status", "code", "field"),
    [
        ({"content": b"{", "headers": {**KEY, "Content-Type": "application/json"}},
         400, "invalid_json", "body"),
        ({"content": b'{"dataset_id":"first","dataset_id":"second"}',
          "headers": {**KEY, "Content-Type": "application/json"}}, 400, "invalid_json", "body"),
        ({"content": (chunk for chunk in [b" " * 4096, b" " * 4096, b" "])},
         413, "request_too_large", "body"),
        ({"content": b"{}", "headers": {"Content-Type": "text/plain"}},
         415, "unsupported_media_type", "header.Content-Type"),
        ({"json": INPUT, "headers": {**KEY, "Content-Encoding": "gzip"}},
         415, "unsupported_media_type", "header.Content-Encoding"),
        ({"json": INPUT}, 422, "invalid_request", "header.Idempotency-Key"),
        ({"json": INPUT, "headers": [("Idempotency-Key", "a"), ("Idempotency-Key", "b")]},
         422, "invalid_request", "header.Idempotency-Key"),
        ({"json": {**INPUT, "operation": "unknown"}, "headers": KEY},
         422, "unknown_operation", "body.operation"),
        ({"json": {**INPUT, "dataset_id": "missing"}, "headers": KEY},
         422, "unknown_dataset", "body.dataset_id"),
        ({"json": {**INPUT, "dataset_id": -1}, "headers": KEY},
         422, "invalid_request", "body.dataset_id"),
        ({"json": {**INPUT, "state": "done"}, "headers": KEY}, 422, "invalid_request", "body"),
    ],
)
def test_create_errors_use_contract(client, payload, status, code, field):
    response = client.post("/jobs", **payload)
    assert response.status_code == status
    assert response.json()["code"] == code
    assert field in {detail["field"] for detail in response.json()["details"]}


def test_routing_validation_and_unsupported_methods(client):
    missing = f"/jobs/{uuid4()}"
    assert client.get(missing).json()["code"] == "job_not_found"
    assert client.get("/jobs/bad-uuid").json()["code"] == "invalid_request"
    assert client.get(f"{missing}/restart").json()["code"] == "route_not_found"
    rejected = client.patch(missing, json={"state": "done"})
    assert rejected.json()["code"] == "method_not_allowed"
    assert rejected.headers["Allow"] == "GET"


def test_concurrent_replays_create_one_job_and_run_it_once(database):
    barrier = Barrier(8)
    reads = Counter()

    def counted_image(asset):
        reads[asset] += 1
        return demo_image(asset)

    with ContractClient(create_app(read_image=counted_image)) as client:
        def send():
            barrier.wait(timeout=5)
            return submit(client)

        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(lambda _: send(), range(8)))
        assert sorted(response.status_code for response in responses) == [200] * 7 + [202]
        job_ids = {response.json()["id"] for response in responses}
        assert len(job_ids) == 1
        job_id = job_ids.pop()
        job_state(client, f"/jobs/{job_id}", "done")

        conflicting = submit(client, body={**INPUT, "operation": "assess_quality"})
        assert conflicting.status_code == 409
        assert conflicting.json()["code"] == "idempotency_conflict"
        reordered = submit(client, body={"operation": INPUT["operation"], "dataset_id": INPUT["dataset_id"]})
        assert reordered.status_code == 200 and reordered.json()["id"] == job_id
        assert client.get(f"/jobs/{job_id}/result").status_code == 200

    assert reads == {"local-1/10": 1, "local-1/29": 1}
    engine = sqlite_engine(str(database))
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT count(*) FROM jobs").scalar_one() == 1
    engine.dispose()


def test_cancel_pending_and_reject_cancel_running(database):
    started = Event()
    release = Event()

    def hold_source(asset):
        started.set()
        if not release.wait(5):
            raise TimeoutError("Тест не освободил обработчик")
        return demo_image(asset)

    with ContractClient(create_app(read_image=hold_source)) as http:
        try:
            running = submit(http).headers["Location"]
            assert started.wait(5)
            pending = submit(http, key={"Idempotency-Key": "second"}).headers["Location"]
            for path in [running, pending]:
                assert http.get(f"{path}/result").json()["code"] == "result_not_ready"
            assert http.post(f"{running}/cancel").json()["code"] == "job_not_cancellable"
            assert http.post(f"{pending}/cancel", json={}).json()["code"] == "invalid_request"
            assert http.post(f"{pending}/cancel", headers=KEY).json()["code"] == "invalid_request"
            cancelled = http.post(f"{pending}/cancel").json()
            assert cancelled["state"] == "cancelled"
            assert http.post(f"{pending}/cancel").json() == cancelled
            assert http.get(f"{pending}/result").json()["code"] == "job_cancelled"
            assert http.patch(pending, json={"state": "done"}).status_code == 405
            assert http.get(pending).json() == cancelled
        finally:
            release.set()
        job_state(http, running, "done")
        assert http.get(pending).json() == cancelled


def test_failed_job_has_no_successful_result(database):
    with ContractClient(create_app(read_image=lambda asset: b"damaged JPEG")) as http:
        path = submit(http).headers["Location"]
        failed = job_state(http, path, "failed")
        assert failed["error"]["code"] == "invalid_image"
        response = http.get(f"{path}/result")
        assert response.status_code == 409
        assert response.json()["details"] == [{"field": "job.error.code", "reason": "invalid_image"}]


def test_empty_database_requires_and_accepts_migrations(tmp_path, monkeypatch):
    path = tmp_path / "empty.sqlite3"
    monkeypatch.setenv("MEANDME_DB", str(path))
    with ContractClient(create_app()) as http:
        assert http.get("/health").json()["code"] == "storage_unavailable"
        assert submit(http).json()["code"] == "storage_unavailable"

    config = Config(str(ROOT / "alembic.ini"))
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    engine = sqlite_engine(str(path))
    assert set(sa.inspect(engine).get_table_names()) == {"alembic_version", "jobs"}
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == "0001"
    engine.dispose()
    with ContractClient(create_app()) as http:
        assert http.get("/health").status_code == 200


def test_internal_errors_hide_diagnostics():
    class BrokenHealthRepository(MemoryJobRepository):
        def check(self):
            raise RuntimeError("traceback /private/user/file SELECT password")

    with ContractClient(create_app(repository=BrokenHealthRepository()), raise_server_exceptions=False) as http:
        response = http.get("/health")
    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"
    assert "private" not in response.text and "SELECT" not in response.text
    UUID(response.headers["X-Correlation-ID"])


def test_result_write_rolls_back_and_restart_recovers_jobs(database):
    engine = sqlite_engine(str(database))
    store = SQLiteJobRepository(engine)
    # RAISE(FAIL) сохраняет изменение UPDATE внутри транзакции до явного отката.
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TRIGGER reject_result AFTER UPDATE OF state ON jobs
            WHEN NEW.state = 'done'
            BEGIN
                SELECT RAISE(FAIL, 'test only: result write failed');
            END
            """
        )
    with ContractClient(create_app(repository=store)) as http:
        path = submit(http).headers["Location"]
        deadline = monotonic() + 5
        while monotonic() < deadline:
            if http.get("/health").status_code == 503:
                break
            sleep(0.02)
        else:
            pytest.fail("Обработчик не остановился после ошибки записи")

        job = http.get(path).json()
        assert job["state"] == "running"
        assert http.get(f"{path}/result").json()["code"] == "result_not_ready"
        with engine.begin() as connection:
            row = connection.exec_driver_sql(
                "SELECT state, finished_at, result FROM jobs WHERE id = ?", (job["id"],)
            ).one()
            assert tuple(row) == ("running", None, None)
            connection.exec_driver_sql("DROP TRIGGER reject_result")
        queued = submit(http, key={"Idempotency-Key": "still-queued"}).headers["Location"]

    with ContractClient(create_app(repository=store, catalog={})) as restarted:
        failed = restarted.get(path).json()
        assert failed["state"] == "failed"
        assert failed["error"]["code"] == "execution_interrupted"
        assert restarted.get(f"{path}/result").json()["code"] == "job_failed"
        assert job_state(restarted, queued, "done")["image_ids"] == [
            item.image_id for item in DEMO_CATALOG["demo-01"].items
        ]
    engine.dispose()


def test_memory_repository_processes_saved_job():
    with ContractClient(create_app(repository=MemoryJobRepository())) as http:
        path = submit(http).headers["Location"]
        assert job_state(http, path, "done")["operation"] == "find_duplicates"
        assert http.get(f"{path}/result").json()["data"]["image_count"] == 3


def test_pending_limit_and_retry_order(database):
    # Запросы проходят обычный HTTP-слой, очередь оставляем неподвижной для проверки лимита.
    http = ContractClient(create_app())
    try:
        for index in range(20):
            response = submit(http, key={"Idempotency-Key": f"queued-{index}"})
            assert response.status_code == 202
        assert submit(http).json()["code"] == "service_busy"
        assert submit(http, key={"Idempotency-Key": "queued-0"}).status_code == 200
        assert submit(http, key={"Idempotency-Key": "queued-0"}, body={
            **INPUT, "operation": "other",
        }).json()["code"] == "idempotency_conflict"
    finally:
        http.close()
