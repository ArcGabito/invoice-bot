from __future__ import annotations

from datetime import UTC, datetime

import pytest

from invoicebot.application.jobs import QUEUES, TaskRegistry, backoff_seconds
from invoicebot.application.scheduler import configured_schedules
from invoicebot.application.tasks import registry
from invoicebot.domain.cron import CronError, parse_cron
from invoicebot.errors import ConfigError
from invoicebot.infrastructure.queue import InlineQueue, RateLimiter, RateLimitTimeout
from invoicebot.infrastructure.queue.limits import RateLimited
from tests.conftest import make_config


@pytest.mark.parametrize(
    ("expression", "after", "expected"),
    [
        ("*/15 * * * *", "2026-10-31T10:07", "2026-10-31T10:15"),
        ("*/15 * * * *", "2026-10-31T10:15", "2026-10-31T10:30"),
        ("0 6 * * *", "2026-10-31T06:00", "2026-11-01T06:00"),
        ("30 3 1 * *", "2026-10-31T23:00", "2026-11-01T03:30"),
        ("0 7 * * 1-5", "2026-10-31T08:00", "2026-11-02T07:00"),  # sábado → lunes
        ("0 8 * * 0", "2026-10-31T08:00", "2026-11-01T08:00"),  # domingo
        ("0 8 * * 7", "2026-10-31T08:00", "2026-11-01T08:00"),  # 7 también es domingo
        ("5 8,13,18 * * *", "2026-10-31T13:05", "2026-10-31T18:05"),
        ("0 0 29 2 *", "2026-03-01T00:00", "2028-02-29T00:00"),
        # Día del mes y de la semana: basta con que coincida uno (como cron).
        ("0 9 1 * 1", "2026-11-01T10:00", "2026-11-02T09:00"),
    ],
)
def test_cron_next(expression: str, after: str, expected: str):
    cron = parse_cron(expression)
    moment = datetime.fromisoformat(after).replace(tzinfo=UTC)
    assert cron.next_after(moment, "UTC").strftime("%Y-%m-%dT%H:%M") == expected


def test_cron_uses_company_timezone():
    # 06:00 en Lima son las 11:00 UTC.
    nxt = parse_cron("0 6 * * *").next_after(datetime(2026, 10, 31, 12, tzinfo=UTC), "America/Lima")
    assert nxt.astimezone(UTC) == datetime(2026, 11, 1, 11, tzinfo=UTC)


@pytest.mark.parametrize(
    "expression", ["* * * *", "60 * * * *", "*/0 * * * *", "a * * * *", "5-1 * * * *", "0 0 31 2 *"]
)
def test_cron_rejects_invalid(expression: str):
    with pytest.raises(CronError):
        parse_cron(expression).next_after(datetime(2026, 1, 1, tzinfo=UTC))


def test_backoff_grows_and_caps():
    assert [backoff_seconds(n) for n in (1, 2, 3, 4)] == [15, 30, 60, 120]
    assert backoff_seconds(30) == 3600


def test_registry_validates_queue_and_names():
    local = TaskRegistry()
    with pytest.raises(ValueError):
        local.task("x", queue="nope")(lambda ctx, p: None)
    with pytest.raises(ConfigError, match="Tarea desconocida"):
        registry.get("no.existe")
    assert set(registry.queues()) <= set(QUEUES)
    assert registry.get("documents.process").queue == "extract"


def test_inline_queue_runs_now_but_not_delayed():
    ran: list[int] = []
    queue = InlineQueue(ran.append)
    assert queue.publish({"id": 7}) is True
    assert queue.publish({"id": 8}, delay_s=10) is False
    assert ran == [7]


def test_local_rate_limiter_waits_and_times_out():
    limiter = RateLimiter("test", 2)
    with limiter.slot():
        pass
    with limiter.slot():
        pass
    with pytest.raises(RateLimitTimeout), limiter.slot(timeout_s=0):
        pass
    unlimited = RateLimiter("test", 0)
    for _ in range(10):
        with unlimited.slot():
            pass


def test_rate_limited_proxy_wraps_only_listed_methods():
    class Client:
        name = "x"

        def call(self) -> str:
            return "ok"

        def other(self) -> str:
            return "otro"

    limiter = RateLimiter("proxy", 1)
    proxy = RateLimited(Client(), limiter, ("call",))
    assert proxy.name == "x"
    assert proxy.call() == "ok"
    assert proxy.other() == "otro"  # no consume el límite
    assert limiter._try() is False  # la única llamada del minuto ya se usó


def test_queue_config_and_schedules(tmp_path):
    config = make_config(tmp_path)
    assert config.queue.backend == "inline"
    names = {s["name"] for s in configured_schedules(config)}
    assert {"collect", "retry", "backup", "purge"} <= names
    assert "sunat" not in names and "cycle" not in names and "fx" not in names

    redis = make_config(
        tmp_path,
        {
            "INVOICEBOT_REDIS_URL": "redis://localhost:6379/0",
            "INVOICEBOT_SCHEDULE_CYCLE": "0 * * * *",
            "INVOICEBOT_SCHEDULE_RETRY": "off",
            "INVOICEBOT_RUN_EXPORT": "xlsx",
        },
    )
    assert redis.queue.backend == "redis"
    names = {s["name"] for s in configured_schedules(redis)}
    assert {"cycle", "export"} <= names and "retry" not in names

    with pytest.raises(ConfigError, match="INVOICEBOT_SCHEDULE_FX"):
        make_config(tmp_path, {"INVOICEBOT_SCHEDULE_FX": "cada hora"})
    with pytest.raises(ConfigError, match="requiere INVOICEBOT_REDIS_URL"):
        make_config(tmp_path, {"INVOICEBOT_QUEUE_BACKEND": "redis"})
