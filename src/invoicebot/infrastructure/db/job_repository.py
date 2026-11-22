"""Trabajos en cola (`jobs`) y tareas programadas (`schedules`) en SQL Server.

SQL Server es la fuente de verdad de cada trabajo: Redis solo lleva su id. Así, si
Redis se reinicia o un mensaje se pierde, el programador vuelve a publicar los
trabajos que quedaron sin enviar (`published_at` NULL).
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Connection, bindparam, text
from sqlalchemy.exc import IntegrityError

from invoicebot.infrastructure.db.connection import Database
from invoicebot.infrastructure.db.invoice_repository import Row, as_row

#: Estados en los que el trabajo todavía puede ejecutarse.
ACTIVE_STATUSES = ("queued", "running", "retrying")
FINAL_STATUSES = ("succeeded", "failed", "cancelled")


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

    @contextmanager
    def _c(self) -> Iterator[Connection]:
        with self.db.begin() as conn:
            yield conn

    def _one(self, sql: str, params: dict[str, Any] | None = None) -> Row | None:
        with self._c() as conn:
            row = conn.execute(text(sql), params or {}).first()
            return _decode(as_row(row._mapping)) if row is not None else None

    def _all(self, sql: str, params: dict[str, Any] | None = None, **bind: Any) -> list[Row]:
        statement = text(sql)
        for name in bind.get("expanding", ()):
            statement = statement.bindparams(bindparam(name, expanding=True))
        with self._c() as conn:
            return [
                _decode(as_row(r._mapping)) or {} for r in conn.execute(statement, params or {})
            ]

    def _exec(self, sql: str, params: dict[str, Any] | None = None) -> int:
        with self._c() as conn:
            return int(conn.execute(text(sql), params or {}).rowcount or 0)

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
                """INSERT INTO jobs (task, queue, status, payload, max_attempts, dedupe_key,
                     idempotency_key, parent_id, created_by, run_after)
                   OUTPUT inserted.*
                   VALUES (:task, :queue, N'queued', :payload, :max, :dedupe, :idem, :parent,
                     :by, :after)""",
                {
                    "task": task,
                    "queue": queue,
                    "payload": _json(payload or {}),
                    "max": max_attempts,
                    "dedupe": dedupe_key,
                    "idem": idempotency_key,
                    "parent": parent_id,
                    "by": (created_by or "")[:150] or None,
                    "after": _utc(run_after),
                },
            )
        except IntegrityError:
            # Otro proceso lo creó a la vez: gana el suyo.
            existing = self._existing(dedupe_key, idempotency_key)
            if existing is None:
                raise
            return existing, False
        assert row is not None
        return row, True

    def _existing(self, dedupe_key: str | None, idempotency_key: str | None) -> Row | None:
        if idempotency_key:
            row = self._one("SELECT * FROM jobs WHERE idempotency_key = :k", {"k": idempotency_key})
            if row is not None:
                return row
        if dedupe_key:
            return self._one("SELECT * FROM jobs WHERE dedupe_key = :k", {"k": dedupe_key})
        return None

    def get_by_idempotency_key(self, key: str) -> Row | None:
        return self._one("SELECT * FROM jobs WHERE idempotency_key = :k", {"k": key})

    def get(self, job_id: int) -> Row | None:
        return self._one("SELECT * FROM jobs WHERE id = :id", {"id": job_id})

    def mark_published(self, job_id: int, *, attempts: int) -> None:
        """Anota el envío solo si el trabajo sigue como se envió (mismo número de intentos).

        Si ya lo ejecutó alguien (la cola inline lo hace al momento, o un worker muy
        rápido) y quedó esperando un reintento, debe seguir sin publicar para que el
        programador lo reenvíe.
        """
        self._exec(
            """UPDATE jobs SET published_at = SYSUTCDATETIME()
               WHERE id = :id AND published_at IS NULL AND attempts = :a
                 AND status IN (N'queued', N'retrying')""",
            {"id": job_id, "a": attempts},
        )

    def claim(self, job_id: int, worker: str) -> Row | None:
        """Pasa el trabajo a "running" y suma un intento; None si ya terminó o se canceló.

        Acepta un trabajo "running": la cola solo lo reentrega si el worker anterior murió.
        """
        return self._one(
            """UPDATE jobs SET status = N'running', attempts = attempts + 1, worker = :w,
                 started_at = SYSUTCDATETIME(), updated_at = SYSUTCDATETIME()
               OUTPUT inserted.*
               WHERE id = :id AND status IN (N'queued', N'retrying', N'running')""",
            {"id": job_id, "w": worker[:200]},
        )

    def succeed(self, job_id: int, result: Any) -> None:
        self._exec(
            """UPDATE jobs SET status = N'succeeded', result = :r, error_message = NULL,
                 dedupe_key = NULL, finished_at = SYSUTCDATETIME(), updated_at = SYSUTCDATETIME()
               WHERE id = :id""",
            {"id": job_id, "r": _json(result)},
        )

    def fail(self, job_id: int, error: str, *, retry_at: datetime | None) -> None:
        """Con ``retry_at`` queda "retrying" y sin publicar; sin él, falla definitivamente."""
        if retry_at is None:
            self._exec(
                """UPDATE jobs SET status = N'failed', error_message = :e, dedupe_key = NULL,
                     finished_at = SYSUTCDATETIME(), updated_at = SYSUTCDATETIME()
                   WHERE id = :id""",
                {"id": job_id, "e": error[:4000]},
            )
        else:
            self._exec(
                """UPDATE jobs SET status = N'retrying', error_message = :e, run_after = :at,
                     published_at = NULL, updated_at = SYSUTCDATETIME()
                   WHERE id = :id""",
                {"id": job_id, "e": error[:4000], "at": _utc(retry_at)},
            )

    def defer(self, job_id: int, run_after: datetime, reason: str) -> None:
        """Lo deja para más tarde sin gastar un intento (p. ej. límite de llamadas)."""
        self._exec(
            """UPDATE jobs SET status = N'retrying', attempts = attempts - 1, run_after = :at,
                 error_message = :e, published_at = NULL, updated_at = SYSUTCDATETIME()
               WHERE id = :id""",
            {"id": job_id, "at": _utc(run_after), "e": reason[:4000]},
        )

    def cancel(self, job_id: int) -> bool:
        return (
            self._exec(
                """UPDATE jobs SET status = N'cancelled', dedupe_key = NULL,
                     finished_at = SYSUTCDATETIME(), updated_at = SYSUTCDATETIME()
                   WHERE id = :id AND status IN (N'queued', N'retrying')""",
                {"id": job_id},
            )
            > 0
        )

    def requeue(self, job_id: int, *, extra_attempts: int) -> Row | None:
        """Vuelve a encolar un trabajo fallido o cancelado (con más intentos si hace falta)."""
        return self._one(
            """UPDATE jobs SET status = N'queued', published_at = NULL, run_after = NULL,
                 finished_at = NULL, max_attempts = CASE WHEN attempts + :n > max_attempts
                   THEN attempts + :n ELSE max_attempts END,
                 updated_at = SYSUTCDATETIME()
               OUTPUT inserted.*
               WHERE id = :id AND status IN (N'failed', N'cancelled')""",
            {"id": job_id, "n": extra_attempts},
        )

    def due_unpublished(self, *, now: datetime, grace_s: int, limit: int = 500) -> list[Row]:
        """Trabajos pendientes de enviar a la cola: nuevos que no se publicaron (tras un margen,
        por si su creador aún lo está haciendo) y reintentos cuya hora llegó."""
        return self._all(
            """SELECT TOP (:n) * FROM jobs
               WHERE status IN (N'queued', N'retrying') AND published_at IS NULL
                 AND COALESCE(run_after, DATEADD(SECOND, :grace, created_at)) <= :now
               ORDER BY id""",
            {"n": limit, "grace": grace_s, "now": _utc(now)},
        )

    def list_jobs(
        self,
        *,
        status: str | None = None,
        task: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Row]:
        where: list[str] = []
        params: dict[str, Any] = {"n": limit, "o": offset}
        if status:
            where.append("status = :s")
            params["s"] = status
        if task:
            where.append("task = :t")
            params["t"] = task
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        return self._all(
            f"""SELECT id, task, queue, status, attempts, max_attempts, error_message, parent_id, worker,
                  created_by, created_at, started_at, finished_at, run_after
                FROM jobs {clause} ORDER BY id DESC
                OFFSET :o ROWS FETCH NEXT :n ROWS ONLY""",
            params,
        )

    def children(self, parent_id: int) -> dict[str, int]:
        rows = self._all(
            "SELECT status, COUNT(*) AS n FROM jobs WHERE parent_id = :p GROUP BY status",
            {"p": parent_id},
        )
        return {r["status"]: int(r["n"]) for r in rows}

    def counts(self) -> list[Row]:
        """Trabajos por tarea y estado (métricas). Los terminados, solo de las últimas 24 h."""
        return self._all(
            """SELECT task, status, COUNT(*) AS n FROM jobs
               WHERE status IN (N'queued', N'running', N'retrying')
                  OR finished_at >= DATEADD(HOUR, -24, SYSUTCDATETIME())
               GROUP BY task, status"""
        )

    def oldest_waiting_seconds(self) -> float | None:
        row = self._one(
            """SELECT DATEDIFF_BIG(MILLISECOND, MIN(COALESCE(run_after, created_at)),
                 SYSUTCDATETIME()) AS ms
               FROM jobs WHERE status IN (N'queued', N'retrying')
                 AND COALESCE(run_after, created_at) <= SYSUTCDATETIME()"""
        )
        if row is None or row["ms"] is None:
            return None
        return max(0.0, int(row["ms"]) / 1000)

    def purge(self, *, older_than_days: int) -> int:
        return self._exec(
            """DELETE FROM jobs WHERE status IN (N'succeeded', N'cancelled', N'failed')
                 AND finished_at < DATEADD(DAY, -:d, SYSUTCDATETIME())""",
            {"d": older_than_days},
        )

    # --- Programación -----------------------------------------------------------

    def sync_schedules(self, schedules: Sequence[dict[str, Any]]) -> None:
        """Alinea las programaciones `config` con la configuración actual.

        Una programación cuyo cron cambió se recalcula; las que ya no están configuradas
        se desactivan (no se borran: conservan su historial).
        """
        names = [s["name"] for s in schedules]
        with self._c() as conn:
            for item in schedules:
                conn.execute(
                    text(
                        """MERGE schedules AS t
                           USING (SELECT :name AS name) AS s ON t.name = s.name
                           WHEN MATCHED AND t.source = N'config' THEN UPDATE SET
                             task = :task, payload = :payload, enabled = 1,
                             next_run_at = CASE WHEN t.cron = :cron AND t.enabled = 1
                               THEN t.next_run_at ELSE NULL END,
                             cron = :cron, updated_at = SYSUTCDATETIME()
                           WHEN NOT MATCHED THEN INSERT (name, task, cron, payload, source)
                             VALUES (:name, :task, :cron, :payload, N'config');"""
                    ),
                    {
                        "name": item["name"],
                        "task": item["task"],
                        "cron": item["cron"],
                        "payload": _json(item.get("payload") or {}),
                    },
                )
            statement = text(
                """UPDATE schedules SET enabled = 0, updated_at = SYSUTCDATETIME()
                   WHERE source = N'config' AND enabled = 1"""
                + (" AND name NOT IN :names" if names else "")
            )
            if names:
                statement = statement.bindparams(bindparam("names", expanding=True))
            conn.execute(statement, {"names": names} if names else {})

    def list_schedules(self) -> list[Row]:
        return self._all("SELECT * FROM schedules ORDER BY name")

    def get_schedule(self, name: str) -> Row | None:
        return self._one("SELECT * FROM schedules WHERE name = :n", {"n": name})

    def set_schedule_next(
        self, name: str, *, next_run_at: datetime, ran_at: datetime | None, job_id: int | None
    ) -> None:
        self._exec(
            """UPDATE schedules SET next_run_at = :next,
                 last_run_at = COALESCE(:ran, last_run_at),
                 last_job_id = COALESCE(:job, last_job_id), updated_at = SYSUTCDATETIME()
               WHERE name = :n""",
            {"n": name, "next": _utc(next_run_at), "ran": _utc(ran_at), "job": job_id},
        )

    def upsert_manual_schedule(
        self, *, name: str, task: str, cron: str, payload: dict[str, Any] | None
    ) -> None:
        self._exec(
            """MERGE schedules AS t USING (SELECT :name AS name) AS s ON t.name = s.name
               WHEN MATCHED THEN UPDATE SET task = :task, cron = :cron, payload = :payload,
                 source = N'manual', enabled = 1, next_run_at = NULL, updated_at = SYSUTCDATETIME()
               WHEN NOT MATCHED THEN INSERT (name, task, cron, payload, source)
                 VALUES (:name, :task, :cron, :payload, N'manual');""",
            {"name": name, "task": task, "cron": cron, "payload": _json(payload or {})},
        )

    def set_schedule_enabled(self, name: str, enabled: bool) -> bool:
        return (
            self._exec(
                # A mano deja de seguir a INVOICEBOT_SCHEDULE_* (si no, la configuración la revertiría).
                """UPDATE schedules SET enabled = :e, next_run_at = NULL, source = N'manual',
                     updated_at = SYSUTCDATETIME() WHERE name = :n""",
                {"n": name, "e": 1 if enabled else 0},
            )
            > 0
        )


def utcnow() -> datetime:
    return datetime.now(UTC)


def later(seconds: float) -> datetime:
    return utcnow() + timedelta(seconds=seconds)
