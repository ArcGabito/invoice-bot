"""Trabajos en cola, programador y worker: SQL Server real (y Redis real si está definido)."""

from __future__ import annotations

import logging
import os
import secrets
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from invoicebot.api.app import create_app
from invoicebot.application.jobs import Jobs, PermanentJobError, RetryLater, TaskRegistry
from invoicebot.application.scheduler import SCHEDULER_LOCK, tick
from invoicebot.context import AppContext
from invoicebot.infrastructure.db.job_repository import JobRepository
from invoicebot.infrastructure.db.lock import run_lock
from invoicebot.infrastructure.queue import InlineQueue
from tests import builders
from tests.conftest import make_config

pytestmark = pytest.mark.db


@pytest.fixture
def context(tmp_path: Path, db_env, clean_db) -> AppContext:
    ctx = AppContext(make_config(tmp_path, db_env=db_env))
    yield ctx
    ctx.close()


def _invoices(ctx: AppContext) -> int:
    with ctx.db.connect() as conn:
        return int(conn.exec_driver_sql("SELECT COUNT(*) FROM invoices").scalar() or 0)


def _jobs_with(ctx: AppContext, registry: TaskRegistry) -> Jobs:
    holder: dict[str, Jobs] = {}
    jobs = Jobs(
        repository=JobRepository(ctx.db),
        queue=InlineQueue(lambda job_id: holder["jobs"].execute(job_id)),  # noqa: PLW0108 (se crea después)
        registry=registry,
        context=ctx,
        default_max_attempts=3,
        logger=logging.getLogger("invoicebot.tests"),
    )
    holder["jobs"] = jobs
    return jobs


def test_repository_dedupe_idempotency_and_lifecycle(clean_db):
    repo = JobRepository(clean_db)
    first, created = repo.create(
        task="t", queue="default", payload={"a": 1}, max_attempts=2, dedupe_key="k"
    )
    assert created and first["status"] == "queued" and first["payload"] == {"a": 1}
    again, created = repo.create(
        task="t", queue="default", payload={}, max_attempts=2, dedupe_key="k"
    )
    assert not created and again["id"] == first["id"]

    claimed = repo.claim(first["id"], "w1")
    assert claimed["status"] == "running" and claimed["attempts"] == 1
    repo.succeed(first["id"], {"ok": True})
    done = repo.get(first["id"])
    assert (
        done["status"] == "succeeded"
        and done["result"] == {"ok": True}
        and done["dedupe_key"] is None
    )
    assert repo.claim(first["id"], "w2") is None  # terminado: no se vuelve a ejecutar

    # Terminado el anterior, la misma clave admite un trabajo nuevo.
    _, created = repo.create(task="t", queue="default", payload={}, max_attempts=2, dedupe_key="k")
    assert created

    idem, created = repo.create(
        task="t", queue="default", payload={}, max_attempts=1, idempotency_key="i"
    )
    same, created_again = repo.create(
        task="t", queue="default", payload={}, max_attempts=1, idempotency_key="i"
    )
    assert created and not created_again and same["id"] == idem["id"]
    assert repo.get_by_idempotency_key("i")["id"] == idem["id"]

    now = datetime.now(UTC)
    repo.claim(idem["id"], "w")
    repo.fail(idem["id"], "temporal", retry_at=now - timedelta(seconds=1))
    assert repo.get(idem["id"])["status"] == "retrying"
    assert idem["id"] in {j["id"] for j in repo.due_unpublished(now=now, grace_s=0)}
    repo.claim(idem["id"], "w")
    repo.defer(idem["id"], now + timedelta(hours=1), "límite")
    deferred = repo.get(idem["id"])
    assert deferred["attempts"] == 1 and deferred["status"] == "retrying"  # no gasta intento
    assert idem["id"] not in {j["id"] for j in repo.due_unpublished(now=now, grace_s=0)}
    assert repo.cancel(idem["id"])
    requeued = repo.requeue(idem["id"], extra_attempts=2)
    assert requeued["status"] == "queued" and requeued["max_attempts"] == 3
    assert {r["status"] for r in repo.counts()} >= {"queued", "succeeded"}


def test_execute_success_retry_permanent_and_children(context: AppContext):
    calls: dict[str, int] = {"flaky": 0}
    registry = TaskRegistry()

    @registry.task("ok")
    def ok(ctx: Any, payload: dict[str, Any]) -> dict[str, Any]:
        child = ctx_jobs.enqueue("child", {"n": payload["n"]})
        return {"double": payload["n"] * 2, "child": child.id}

    @registry.task("child")
    def child(ctx: Any, payload: dict[str, Any]) -> int:
        return payload["n"]

    @registry.task("flaky")
    def flaky(ctx: Any, payload: dict[str, Any]) -> str:
        calls["flaky"] += 1
        raise RuntimeError("servicio caído")

    @registry.task("bad")
    def bad(ctx: Any, payload: dict[str, Any]) -> None:
        raise PermanentJobError("datos no válidos")

    @registry.task("busy")
    def busy(ctx: Any, payload: dict[str, Any]) -> None:
        raise RetryLater("ocupado", delay_s=60)

    ctx_jobs = _jobs_with(context, registry)
    repo = ctx_jobs.repository

    done = ctx_jobs.enqueue("ok", {"n": 21})
    job = repo.get(done.id)
    assert job["status"] == "succeeded" and job["result"]["double"] == 42
    assert repo.get(job["result"]["child"])["parent_id"] == done.id
    assert repo.children(done.id) == {"succeeded": 1}

    flaky_job = ctx_jobs.enqueue("flaky")
    row = repo.get(flaky_job.id)
    assert row["status"] == "retrying" and row["published_at"] is None and row["run_after"]
    # El programador lo reintenta cuando vence la espera, hasta agotar los intentos.
    for _ in range(5):
        ctx_jobs.relay(now=datetime.now(UTC) + timedelta(days=1))
    row = repo.get(flaky_job.id)
    assert row["status"] == "failed" and row["attempts"] == 3 and calls["flaky"] == 3

    assert repo.get(ctx_jobs.enqueue("bad").id)["status"] == "failed"
    busy_row = repo.get(ctx_jobs.enqueue("busy").id)
    assert busy_row["status"] == "retrying" and busy_row["attempts"] == 0


def test_queue_mode_scan_reads_xml_here_and_enqueues_the_rest(context: AppContext, tmp_path: Path):
    inbox = tmp_path / "entrada"
    builders.write(inbox / "20100047218-01-F001-00001234.xml", builders.ubl_invoice())
    builders.write(
        inbox / "20100047218-01-F001-00001234.pdf", builders.invoice_pdf(number="00001234")
    )
    builders.write(inbox / "otra.pdf", builders.invoice_pdf(number="00000777"))

    result = context.jobs.enqueue("documents.scan", {"root": str(inbox)})
    scan = context.job_repository.get(result.id)
    assert scan["status"] == "succeeded", scan["error_message"]
    summary = scan["result"]
    assert summary["from_xml"] == 1 and summary["linked_to_xml"] == 1 and summary["dispatched"] == 1
    children = context.job_repository.list_jobs(task="documents.process")
    assert len(children) == 1 and children[0]["parent_id"] == result.id
    assert children[0]["status"] == "succeeded"
    assert _invoices(context) == 2

    # Repetir el escaneo no vuelve a encolar nada: todo está procesado.
    again = context.job_repository.get(
        context.jobs.enqueue("documents.scan", {"root": str(inbox)}).id
    )
    assert again["result"]["dispatched"] == 0 and again["result"]["already_processed"] == 3


def test_scheduler_tick_enqueues_once_and_respects_lock(context: AppContext):
    repo = context.job_repository
    now = datetime(2026, 10, 31, 10, 7, tzinfo=UTC)
    first = tick(context, now=now)
    assert first.enqueued == [] and not first.errors  # primera vez: solo calcula la próxima
    schedules = {s["name"]: s for s in repo.list_schedules()}
    assert schedules["retry"]["next_run_at"] == datetime(2026, 10, 31, 10, 30, tzinfo=UTC)

    later = tick(context, now=datetime(2026, 10, 31, 10, 31, tzinfo=UTC))
    names = {item["schedule"] for item in later.enqueued}
    assert "retry" in names and "collect" in names
    retry_job = repo.get(repo.get_schedule("retry")["last_job_id"])
    assert retry_job["created_by"] == "programador:retry"

    repo.set_schedule_enabled("backup", False)
    assert repo.get_schedule("backup")["source"] == "manual"
    tick(context, now=datetime(2026, 11, 1, 3, 0, tzinfo=UTC))
    assert not repo.get_schedule("backup")["enabled"]  # la configuración no la reactiva

    with run_lock(context.db, SCHEDULER_LOCK):
        assert tick(context).skipped


def test_api_jobs_idempotency_events_and_metrics(context: AppContext):
    write_key, _ = context.users.create_api_key(name="erp", scope="write", created_by="t")
    headers = {"authorization": f"Bearer {write_key}"}
    with TestClient(create_app(context), base_url="https://invoicebot.test") as client:
        files = [("files", ("F001-00001234.xml", builders.ubl_invoice(), "application/xml"))]
        res = client.post(
            "/api/v1/documents", files=files, headers={**headers, "Idempotency-Key": "lote-1"}
        )
        assert res.status_code == 201, res.text
        job_id = res.json()["job"]["id"]
        assert res.json()["summary"]["from_xml"] == 1
        again = client.post(
            "/api/v1/documents", files=files, headers={**headers, "Idempotency-Key": "lote-1"}
        )
        assert again.status_code == 200 and again.json()["replayed"]
        assert again.json()["job"]["id"] == job_id

        assert client.get(f"/api/v1/jobs/{job_id}", headers=headers).json()["status"] == "succeeded"
        assert client.get("/api/v1/jobs?task=documents.scan", headers=headers).json()["items"]
        with client.stream("GET", f"/api/v1/jobs/{job_id}/events", headers=headers) as stream:
            body = "".join(stream.iter_text())
        assert "event: job" in body and "event: end" in body

        created = client.post(
            "/api/v1/jobs", json={"task": "jobs.purge", "payload": {"days": 1}}, headers=headers
        )
        assert created.status_code == 202 and created.json()["job"]["status"] == "succeeded"
        assert (
            client.post("/api/v1/jobs", json={"task": "nope"}, headers=headers).status_code == 400
        )

        ready = client.get("/readyz").json()
        assert ready["queue"]["backend"] == "inline"
        assert client.get("/metrics").status_code == 401
        metrics = client.get("/metrics", headers=headers).text
        assert 'invoicebot_jobs{task="documents.scan",status="succeeded"}' in metrics
        assert client.get("/api/v1/schedules", headers=headers).status_code == 200


# --- Redis real ------------------------------------------------------------------------


@pytest.fixture
def redis_url() -> str:
    url = os.environ.get("INVOICEBOT_TEST_REDIS_URL")
    if not url:
        if os.environ.get("INVOICEBOT_TEST_REDIS_REQUIRED") == "true":
            pytest.fail("Falta INVOICEBOT_TEST_REDIS_URL")
        pytest.skip("Sin Redis de pruebas: define INVOICEBOT_TEST_REDIS_URL")
    return url


@pytest.mark.redis
def test_redis_worker_processes_queue_end_to_end(tmp_path: Path, db_env, clean_db, redis_url: str):
    from invoicebot.infrastructure.queue.redis_queue import RedisQueue, run_worker

    namespace = f"invoicebot-test-{secrets.token_hex(3)}"
    config = make_config(
        tmp_path,
        {
            "INVOICEBOT_REDIS_URL": redis_url,
            "INVOICEBOT_QUEUE_NAMESPACE": namespace,
            "INVOICEBOT_AI_RATE_PER_MINUTE": "100",
        },
        db_env=db_env,
    )
    ctx = AppContext(config)
    inbox = tmp_path / "entrada"
    for n in range(4):
        builders.write(inbox / f"f{n}.pdf", builders.invoice_pdf(number=f"0000{n}100"))
    stop = threading.Event()
    try:
        assert ctx.queue.ping().startswith("Redis")
        scan = ctx.jobs.enqueue("documents.scan", {"root": str(inbox)})
        assert ctx.job_repository.get(scan.id)["status"] == "queued"  # nadie lo ejecutó aún
        queue = ctx.queue
        assert isinstance(queue, RedisQueue)
        assert queue.sizes()["ingest"]["ready"] == 1

        worker = threading.Thread(
            target=run_worker,
            kwargs={
                "queue": queue,
                "queues": ["ingest", "extract"],
                "threads": 2,
                "logger": logging.getLogger("invoicebot.tests"),
                "stop_event": stop,
            },
            daemon=True,
        )
        worker.start()
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            jobs = ctx.job_repository.list_jobs(limit=50)
            if len(jobs) == 5 and all(j["status"] == "succeeded" for j in jobs):
                break
            time.sleep(0.5)
        jobs = ctx.job_repository.list_jobs(limit=50)
        assert len(jobs) == 5 and {j["status"] for j in jobs} == {"succeeded"}, jobs
        assert _invoices(ctx) == 4
        assert {j["worker"].split(":")[0] for j in jobs} == {os.uname().nodename}
        assert sum(v["ready"] for v in queue.sizes().values()) == 0
    finally:
        stop.set()
        time.sleep(1.5)
        for key in ctx.redis.scan_iter(f"{namespace}*"):
            ctx.redis.delete(key)
        ctx.close()
