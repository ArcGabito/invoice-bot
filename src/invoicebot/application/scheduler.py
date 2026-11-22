"""Programador: encola las tareas programadas cuando les toca y reenvía lo pendiente.

Un "tick" hace tres cosas, con un candado de SQL Server para que nunca corran dos
a la vez (aunque haya varias réplicas o un Container Apps Job se solape):

1. Alinea la tabla `schedules` con INVOICEBOT_SCHEDULE_* (las manuales se respetan).
2. Encola cada programación vencida. Si su trabajo anterior sigue activo, no se
   duplica (clave `schedule:<nombre>`). Tras una caída no se recuperan las
   ejecuciones perdidas: se ejecuta una vez y se calcula la siguiente.
3. Publica los trabajos que quedaron sin enviar y los reintentos vencidos.

`invoicebot scheduler` repite el tick cada 30 s (VPS); `invoicebot schedule tick`
hace uno y termina (Container Apps Job programado o tarea de Windows).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from invoicebot.domain.cron import CronError, parse_cron
from invoicebot.errors import LockedError, error_message

if TYPE_CHECKING:
    from invoicebot.config import Config
    from invoicebot.context import AppContext

SCHEDULER_LOCK = "invoicebot:scheduler"


def configured_schedules(config: Config) -> list[dict[str, Any]]:
    """Las programaciones que se derivan de la configuración."""
    s = config.schedule
    wanted: list[tuple[str, str | None, str, bool]] = [
        ("cycle", s.cycle, "bot.cycle", True),
        (
            "collect",
            s.collect,
            "inbox.collect",
            bool(config.run.inbox_dir) or config.mail.provider != "none",
        ),
        ("fx", s.fx, "fx.fetch", config.fx.sunat),
        ("sunat", s.sunat, "sunat.verify", config.sunat.enabled),
        ("retry", s.retry, "documents.retry", True),
        ("export", s.export, "exports.monthly", config.run.export != "none"),
        ("backup", s.backup, "db.backup", True),
        ("purge", s.purge, "jobs.purge", True),
        ("approvals", s.approvals, "approvals.remind", config.purchasing.enabled),
        ("webhooks", s.webhooks, "webhooks.sweep", True),
        ("reverify", s.reverify, "sunat.reverify", config.sunat.enabled),
        ("compliance", s.compliance, "compliance.alerts", True),
    ]
    return [
        {"name": name, "cron": cron, "task": task, "payload": {}}
        for name, cron, task, enabled in wanted
        if cron and enabled
    ]


@dataclass
class TickResult:
    skipped: str | None = None
    enqueued: list[dict[str, Any]] = field(default_factory=list)
    relayed: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "skipped": self.skipped,
            "enqueued": self.enqueued,
            "relayed": self.relayed,
            "errors": self.errors,
        }


def tick(ctx: AppContext, *, now: datetime | None = None) -> TickResult:
    from invoicebot.infrastructure.db.job_repository import utcnow
    from invoicebot.infrastructure.db.lock import run_lock

    now = now or utcnow()
    result = TickResult()
    repo, jobs, tz = ctx.job_repository, ctx.jobs, ctx.config.timezone
    try:
        with run_lock(ctx.db, SCHEDULER_LOCK):
            repo.sync_schedules(configured_schedules(ctx.config))
            for schedule in repo.list_schedules():
                if not schedule["enabled"]:
                    continue
                name = schedule["name"]
                try:
                    cron = parse_cron(schedule["cron"])
                except CronError as error:
                    result.errors.append(f"{name}: {error}")
                    continue
                next_at = schedule["next_run_at"]
                if next_at is None:
                    repo.set_schedule_next(
                        name, next_run_at=cron.next_after(now, tz), ran_at=None, job_id=None
                    )
                    continue
                if next_at > now:
                    continue
                try:
                    enqueued = jobs.enqueue(
                        schedule["task"],
                        schedule.get("payload") or {},
                        dedupe_key=f"schedule:{name}",
                        created_by=f"programador:{name}",
                    )
                except Exception as error:
                    result.errors.append(f"{name}: {error_message(error)}")
                    ctx.logger.error("no se pudo encolar %s: %s", name, error_message(error))
                    continue
                repo.set_schedule_next(
                    name, next_run_at=cron.next_after(now, tz), ran_at=now, job_id=enqueued.id
                )
                result.enqueued.append(
                    {"schedule": name, "job_id": enqueued.id, "new": enqueued.created}
                )
            result.relayed = jobs.relay(now=now)
    except LockedError:
        result.skipped = "otro programador está en marcha"
    return result


def run_scheduler(
    ctx: AppContext, *, interval_s: float = 30, stop_event: threading.Event | None = None
) -> None:
    """Repite el tick hasta SIGTERM/SIGINT (o ``stop_event``)."""
    import signal

    stop = stop_event or threading.Event()
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
    ctx.logger.info("programador en marcha (cada %.0f s)", interval_s)
    while not stop.is_set():
        try:
            outcome = tick(ctx)
            if outcome.enqueued or outcome.relayed:
                ctx.logger.info(
                    "programador: %s encolados, %s reenviados",
                    len(outcome.enqueued),
                    outcome.relayed,
                )
        except Exception as error:  # SQL Server o Redis caídos: se reintenta en el próximo tick.
            ctx.logger.error("tick del programador fallido: %s", error_message(error))
        stop.wait(interval_s)
    ctx.logger.info("programador detenido")
