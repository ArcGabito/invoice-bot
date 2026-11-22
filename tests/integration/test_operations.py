"""Fase 6: límite de peticiones por clave y trazas de los trabajos."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from invoicebot.api.app import create_app
from invoicebot.context import AppContext
from invoicebot.infrastructure import telemetry
from tests.conftest import make_config

pytestmark = pytest.mark.db


def test_api_rate_limit_per_key(tmp_path: Path, db_env, clean_db):
    ctx = AppContext(make_config(tmp_path, {"INVOICEBOT_API_RATE_PER_MINUTE": "3"}, db_env=db_env))
    try:
        first, _ = ctx.users.create_api_key(name="erp", scope="read", created_by="t")
        second, _ = ctx.users.create_api_key(name="bi", scope="read", created_by="t")
        with TestClient(create_app(ctx), base_url="https://invoicebot.test") as client:
            codes = [
                client.get(
                    "/api/v1/invoices", headers={"authorization": f"Bearer {first}"}
                ).status_code
                for _ in range(4)
            ]
            assert codes == [200, 200, 200, 429]
            limited = client.get("/api/v1/invoices", headers={"authorization": f"Bearer {first}"})
            assert limited.headers["retry-after"] == "60"
            # Cada clave tiene su propio cupo.
            assert (
                client.get(
                    "/api/v1/invoices", headers={"authorization": f"Bearer {second}"}
                ).status_code
                == 200
            )
            assert client.get("/healthz").status_code == 200  # las sondas no cuentan
    finally:
        ctx.close()


def test_job_spans_with_in_memory_exporter(tmp_path: Path, db_env, clean_db, monkeypatch):
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(trace, "get_tracer", lambda name, *a, **k: provider.get_tracer(name))
    monkeypatch.setattr(telemetry, "_ENABLED", True)
    ctx = AppContext(make_config(tmp_path, db_env=db_env))
    try:
        result = ctx.jobs.enqueue("jobs.purge", {"days": 1})
        assert ctx.job_repository.get(result.id)["status"] == "succeeded"
    finally:
        ctx.close()
    spans = [s for s in exporter.get_finished_spans() if s.name == "job jobs.purge"]
    assert spans and spans[0].attributes["job.id"] == result.id
    assert spans[0].attributes["job.queue"] == "default"
