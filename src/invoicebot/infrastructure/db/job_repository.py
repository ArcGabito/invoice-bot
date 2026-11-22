"""Trabajos en cola (`jobs`) y tareas programadas (`schedules`) con SQLAlchemy ORM.

SQL Server es la fuente de verdad de cada trabajo: Redis solo lleva su id. Así, si
Redis se reinicia o un mensaje se pierde, el programador vuelve a publicar los
trabajos que quedaron sin enviar (`published_at` NULL).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Executable, delete, func, insert, literal_column, or_, select, update
from sqlalchemy.exc import IntegrityError

from invoicebot.infrastructure.db.connection import Database
from invoicebot.infrastructure.db.invoice_repository import Row, as_row
from invoicebot.infrastructure.db.models import Job, Schedule

#: Estados en los que el trabajo todavía puede ejecutarse.
ACTIVE_STATUSES = ("queued", "running", "retrying")
FINAL_STATUSES = ("succeeded", "failed", "cancelled")
_NOW = func.sysutcdatetime()

_LIST_COLUMNS = (
    Job.id,
    Job.task,
    Job.queue,
    Job.status,
    Job.attempts,
    Job.max_attempts,
    Job.error_message,
    Job.parent_id,
    Job.worker,
    Job.created_by,
    Job.created_at,
    Job.started_at,
    Job.finished_at,
    Job.run_after,
)


def _utc(value: datetime | None) -> datetime | None:
    """DATETIME2 no guarda zona: se escribe en UTC sin tzinfo."""
    if value is None:
        return None
    return value.astimezone(UTC).replace(tzinfo=None)


def _json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _decode(row: Row | None) -> Row | None:
    if row is None:
        return None
    for column in ("payload", "result"):
        if column in row and isinstance(row[column], str):
            with suppress(ValueError):
                row[column] = json.loads(row[column])
    return row


class JobRepository:
    def __init__(self, db: Database):
        self.db = db

    def _one(self, statement: Executable) -> Row | None:
        with self.db.begin() as conn:
            row = conn.execute(statement).mappings().first()
            return _decode(as_row(row)) if row is not None else None

    def _all(self, statement: Executable) -> list[Row]:
        with self.db.begin() as conn:
            return [_decode(as_row(r)) or {} for r in conn.execute(statement).mappings()]

    def _exec(self, statement: Executable) -> int:
        with self.db.begin() as conn:
            return int(conn.execute(statement).rowcount or 0)

    # --- Trabajos -------------------------------------------------------------

    def create(
        self,
        *,
        task: str,
        queue: str,
        payload: dict[str, Any] | None,
        max_attempts: int,
        dedupe_key: str | None = None,
        idempotency_key: str | None = None,
        parent_id: int | None = None,
        created_by: str | None = None,
        run_after: datetime | None = None,
    ) -> tuple[Row, bool]:
        """Crea el trabajo. Si ya hay uno activo con la misma clave, devuelve ese (False)."""
        existing = self._existing(dedupe_key, idempotency_key)
        if existing is not None:
            return existing, False
        try:
            row = self._one(
                insert(Job)
                .values(
                    task=task,
                    queue=queue,
                    status="queued",
                    payload=_json(payload or {}),
                    max_attempts=max_attempts,
                    dedupe_key=dedupe_key,
                    idempotency_key=idempotency_key,
                    parent_id=parent_id,
                    created_by=(created_by or "")[:150] or None,
                    run_after=_utc(run_after),
                )
                .returning(Job)
            )
        except IntegrityError:
            # Otro proceso lo creó a la vez (índice único filtrado): gana el suyo.
            existing = self._existing(dedupe_key, idempotency_key)
            if existing is None:
                raise
            return existing, False
        assert row is not None
        return row, True

    def _existing(self, dedupe_key: str | None, idempotency_key: str | None) -> Row | None:
        if idempotency_key:
            row = self.get_by_idempotency_key(idempotency_key)
            if row is not None:
                return row
        if dedupe_key:
            return self._one(select(Job).where(Job.dedupe_key == dedupe_key))
        return None

    def get_by_idempotency_key(self, key: str) -> Row | None:
        return self._one(select(Job).where(Job.idempotency_key == key))

    def get(self, job_id: int) -> Row | None:
        return self._one(select(Job).where(Job.id == job_id))

    def mark_published(self, job_id: int, *, attempts: int) -> None:
        """Anota el envío solo si el trabajo sigue como se envió (mismo número de intentos).

        Si ya lo ejecutó alguien (la cola inline lo hace al momento, o un worker muy
        rápido) y quedó esperando un reintento, debe seguir sin publicar para que el
        programador lo reenvíe.
        """
        self._exec(
            update(Job)
            .where(
                Job.id == job_id,
                Job.published_at.is_(None),
                Job.attempts == attempts,
                Job.status.in_(("queued", "retrying")),
            )
            .values(published_at=_NOW)
        )

    def claim(self, job_id: int, worker: str) -> Row | None:
        """Pasa el trabajo a "running" y suma un intento; None si ya terminó o se canceló.

        Acepta un trabajo "running": la cola solo lo reentrega si el worker anterior murió.
        """
        return self._one(
            update(Job)
            .where(Job.id == job_id, Job.status.in_(ACTIVE_STATUSES))
            .values(
                status="running",
                attempts=Job.attempts + 1,
                worker=worker[:200],
                started_at=_NOW,
                updated_at=_NOW,
            )
            .returning(Job)
        )

    def succeed(self, job_id: int, result: Any) -> None:
        self._exec(
            update(Job)
            .where(Job.id == job_id)
            .values(
                status="succeeded",
                result=_json(result),
                error_message=None,
                dedupe_key=None,
                finished_at=_NOW,
                updated_at=_NOW,
            )
        )

    def fail(self, job_id: int, error: str, *, retry_at: datetime | None) -> None:
        """Con ``retry_at`` queda "retrying" y sin publicar; sin él, falla definitivamente."""
        if retry_at is None:
            values: dict[str, Any] = {
                "status": "failed",
                "dedupe_key": None,
                "finished_at": _NOW,
            }
        else:
            values = {"status": "retrying", "run_after": _utc(retry_at), "published_at": None}
        self._exec(
            update(Job)
            .where(Job.id == job_id)
            .values(error_message=error[:4000], updated_at=_NOW, **values)
        )

    def defer(self, job_id: int, run_after: datetime, reason: str) -> None:
        """Lo deja para más tarde sin gastar un intento (p. ej. límite de llamadas)."""
        self._exec(
            update(Job)
            .where(Job.id == job_id)
            .values(
                status="retrying",
                attempts=Job.attempts - 1,
                run_after=_utc(run_after),
                error_message=reason[:4000],
                published_at=None,
                updated_at=_NOW,
            )
        )

    def cancel(self, job_id: int) -> bool:
        return (
            self._exec(
                update(Job)
                .where(Job.id == job_id, Job.status.in_(("queued", "retrying")))
                .values(status="cancelled", dedupe_key=None, finished_at=_NOW, updated_at=_NOW)
            )
            > 0
        )

    def requeue(self, job_id: int, *, extra_attempts: int) -> Row | None:
        """Vuelve a encolar un trabajo fallido o cancelado (con más intentos si hace falta)."""
        return self._one(
            update(Job)
            .where(Job.id == job_id, Job.status.in_(("failed", "cancelled")))
            .values(
                status="queued",
                published_at=None,
                run_after=None,
                finished_at=None,
                max_attempts=func.iif(
                    Job.attempts + extra_attempts > Job.max_attempts,
                    Job.attempts + extra_attempts,
                    Job.max_attempts,
                ),
                updated_at=_NOW,
            )
            .returning(Job)
        )

    def due_unpublished(self, *, now: datetime, grace_s: int, limit: int = 500) -> list[Row]:
        """Trabajos pendientes de enviar a la cola: nuevos que no se publicaron (tras un margen,
        por si su creador aún lo está haciendo) y reintentos cuya hora llegó."""
        due_at = func.coalesce(
            Job.run_after, func.dateadd(literal_column("SECOND"), grace_s, Job.created_at)
        )
        return self._all(
            select(Job)
            .where(
                Job.status.in_(("queued", "retrying")),
                Job.published_at.is_(None),
                due_at <= _utc(now),
            )
            .order_by(Job.id)
            .limit(limit)
        )

    def list_jobs(
        self,
        *,
        status: str | None = None,
        task: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Row]:
        query = select(*_LIST_COLUMNS).order_by(Job.id.desc()).offset(offset).limit(limit)
        if status:
            query = query.where(Job.status == status)
        if task:
            query = query.where(Job.task == task)
        return self._all(query)

    def children(self, parent_id: int) -> dict[str, int]:
        rows = self._all(
            select(Job.status, func.count().label("n"))
            .where(Job.parent_id == parent_id)
            .group_by(Job.status)
        )
        return {r["status"]: int(r["n"]) for r in rows}

    def counts(self) -> list[Row]:
        """Trabajos por tarea y estado (métricas). Los terminados, solo de las últimas 24 h."""
        day_ago = func.dateadd(literal_column("HOUR"), -24, _NOW)
        return self._all(
            select(Job.task, Job.status, func.count().label("n"))
            .where(or_(Job.status.in_(ACTIVE_STATUSES), Job.finished_at >= day_ago))
            .group_by(Job.task, Job.status)
        )

    def oldest_waiting_seconds(self) -> float | None:
        ready_at = func.coalesce(Job.run_after, Job.created_at)
        waited = func.datediff_big(literal_column("MILLISECOND"), func.min(ready_at), _NOW)
        row = self._one(
            select(waited.label("ms")).where(
                Job.status.in_(("queued", "retrying")), ready_at <= _NOW
            )
        )
        if row is None or row["ms"] is None:
            return None
        return max(0.0, int(row["ms"]) / 1000)

    def purge(self, *, older_than_days: int) -> int:
        return self._exec(
            delete(Job).where(
                Job.status.in_(FINAL_STATUSES),
                Job.finished_at < func.dateadd(literal_column("DAY"), -older_than_days, _NOW),
            )
        )

    # --- Programación -----------------------------------------------------------

    def sync_schedules(self, schedules: Sequence[dict[str, Any]]) -> None:
        """Alinea las programaciones `config` con la configuración actual.

        Una programación cuyo cron cambió se recalcula; las que ya no están configuradas
        se desactivan (no se borran: conservan su historial). Las manuales no se tocan.
        """
        names = [s["name"] for s in schedules]
        with self.db.begin() as conn:
            current = {
                r.name: r
                for r in conn.execute(
                    select(Schedule.name, Schedule.cron, Schedule.enabled, Schedule.source)
                )
            }
            for item in schedules:
                payload = _json(item.get("payload") or {})
                found = current.get(item["name"])
                if found is None:
                    # Otro proceso pudo crearla a la vez: la clave primaria lo impide.
                    with suppress(IntegrityError), conn.begin_nested():
                        conn.execute(
                            insert(Schedule).values(
                                name=item["name"],
                                task=item["task"],
                                cron=item["cron"],
                                payload=payload,
                                source="config",
                            )
                        )
                    continue
                if found.source != "config":
                    continue
                values: dict[str, Any] = {
                    "task": item["task"],
                    "cron": item["cron"],
                    "payload": payload,
                    "enabled": True,
                    "updated_at": _NOW,
                }
                if found.cron != item["cron"] or not found.enabled:
                    values["next_run_at"] = None
                conn.execute(update(Schedule).where(Schedule.name == item["name"]).values(**values))
            stale = update(Schedule).where(
                Schedule.source == "config",
                Schedule.enabled == True,  # noqa: E712 (SQL Server: = 1)
            )
            if names:
                stale = stale.where(Schedule.name.not_in(names))
            conn.execute(stale.values(enabled=False, updated_at=_NOW))

    def list_schedules(self) -> list[Row]:
        return self._all(select(Schedule).order_by(Schedule.name))

    def get_schedule(self, name: str) -> Row | None:
        return self._one(select(Schedule).where(Schedule.name == name))

    def set_schedule_next(
        self, name: str, *, next_run_at: datetime, ran_at: datetime | None, job_id: int | None
    ) -> None:
        values: dict[str, Any] = {"next_run_at": _utc(next_run_at), "updated_at": _NOW}
        if ran_at is not None:
            values["last_run_at"] = _utc(ran_at)
        if job_id is not None:
            values["last_job_id"] = job_id
        self._exec(update(Schedule).where(Schedule.name == name).values(**values))

    def upsert_manual_schedule(
        self, *, name: str, task: str, cron: str, payload: dict[str, Any] | None
    ) -> None:
        values = {
            "task": task,
            "cron": cron,
            "payload": _json(payload or {}),
            "source": "manual",
            "enabled": True,
            "next_run_at": None,
        }
        with self.db.begin() as conn:
            updated = conn.execute(
                update(Schedule).where(Schedule.name == name).values(updated_at=_NOW, **values)
            ).rowcount
            if not updated:
                conn.execute(insert(Schedule).values(name=name, **values))

    def set_schedule_enabled(self, name: str, enabled: bool) -> bool:
        # A mano deja de seguir a INVOICEBOT_SCHEDULE_* (si no, la configuración la revertiría).
        return (
            self._exec(
                update(Schedule)
                .where(Schedule.name == name)
                .values(enabled=enabled, next_run_at=None, source="manual", updated_at=_NOW)
            )
            > 0
        )


def utcnow() -> datetime:
    return datetime.now(UTC)


def later(seconds: float) -> datetime:
    return utcnow() + timedelta(seconds=seconds)
