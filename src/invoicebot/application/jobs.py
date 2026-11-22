"""Trabajos en cola: registro de tareas, encolado idempotente y ejecución.

Una tarea es una función ``handler(ctx, payload) -> resultado`` con su cola. El
mismo código corre en dos modos (INVOICEBOT_QUEUE_BACKEND):

- ``inline``: el trabajo se ejecuta en el proceso que lo encola (CLI, Windows, pruebas).
- ``redis``: el trabajo se publica en Redis y lo ejecuta un ``invoicebot worker``
  (Dramatiq), que puede escalar a varias réplicas.

En los dos, SQL Server guarda el estado (tabla ``jobs``): intentos, resultado,
error y reintentos con espera exponencial. La cola solo transporta el id.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from invoicebot.errors import ConfigError, InvoiceBotError, error_message

if TYPE_CHECKING:
    from invoicebot.infrastructure.db.job_repository import JobRepository

#: Colas conocidas, en orden de prioridad para un worker que atiende varias.
QUEUES = ("ingest", "extract", "sunat", "default")


class RetryLater(InvoiceBotError):
    """El trabajo no puede hacerse ahora (límite de llamadas, servicio ocupado): se
    reprograma sin gastar un intento."""

    code = "RETRY_LATER"

    def __init__(self, message: str, *, delay_s: float = 30):
        super().__init__(message)
        self.delay_s = delay_s


class PermanentJobError(InvoiceBotError):
    """Error que no se arregla reintentando (datos no válidos, tarea mal configurada)."""

    code = "JOB_PERMANENT_ERROR"


#: ``handler(ctx, payload)``: ctx es el AppContext del proceso que ejecuta el trabajo.
Handler = Callable[[Any, dict[str, Any]], Any]


@dataclass(frozen=True)
class TaskSpec:
    name: str
    queue: str
    handler: Handler
    description: str = ""
    #: None: INVOICEBOT_JOB_MAX_ATTEMPTS.
    max_attempts: int | None = None
    time_limit_s: int = 30 * 60


@dataclass
class TaskRegistry:
    tasks: dict[str, TaskSpec] = field(default_factory=dict)

    def register(self, spec: TaskSpec) -> TaskSpec:
        if spec.queue not in QUEUES:
            raise ValueError(f"Cola desconocida {spec.queue!r} para {spec.name}")
        self.tasks[spec.name] = spec
        return spec

    def task(
        self, name: str, *, queue: str = "default", description: str = "", **options: Any
    ) -> Callable[[Handler], Handler]:
        def decorate(handler: Handler) -> Handler:
            self.register(TaskSpec(name, queue, handler, description, **options))
            return handler

        return decorate

    def get(self, name: str) -> TaskSpec:
        spec = self.tasks.get(name)
        if spec is None:
            known = ", ".join(sorted(self.tasks))
            raise ConfigError(f"Tarea desconocida {name!r} (disponibles: {known})")
        return spec

    def queues(self) -> list[str]:
        used = {spec.queue for spec in self.tasks.values()}
        return [q for q in QUEUES if q in used]


class JobQueue(Protocol):
    """Transporte de trabajos (inline o Redis)."""

    backend: str

    def publish(self, job: dict[str, Any], *, delay_s: float = 0) -> bool:
        """Envía el trabajo. False si no puede enviarse ahora (lo hará el programador)."""
        ...

    def ping(self) -> str: ...

    def sizes(self) -> dict[str, dict[str, int]]:
        """Mensajes por cola: ``{"extract": {"ready": 3, "dead": 0}}``."""
        ...


class TaskContext(Protocol):
    """Lo que una tarea puede usar (lo implementa invoicebot.context.AppContext)."""

    @property
    def logger(self) -> logging.Logger: ...

    @property
    def jobs(self) -> Jobs: ...


@dataclass
class EnqueueResult:
    job: dict[str, Any]
    created: bool

    @property
    def id(self) -> int:
        return int(self.job["id"])


def backoff_seconds(attempt: int, *, base: float = 15, cap: float = 3600) -> float:
    """15 s, 30 s, 1 min, 2 min… hasta 1 h."""
    return float(min(cap, base * (2 ** max(0, attempt - 1))))


def worker_name() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{threading.current_thread().name}"


class Jobs:
    """Servicio de trabajos: lo usan la CLI, la API, el programador y los workers."""

    def __init__(
        self,
        *,
        repository: JobRepository,
        queue: JobQueue,
        registry: TaskRegistry,
        context: TaskContext,
        default_max_attempts: int,
        logger: logging.Logger,
    ):
        self.repository = repository
        self.queue = queue
        self.registry = registry
        self.context = context
        self.default_max_attempts = default_max_attempts
        self.logger = logger
        self._local = threading.local()

    @property
    def backend(self) -> str:
        return self.queue.backend

    @property
    def current_job_id(self) -> int | None:
        """El trabajo que se está ejecutando en este hilo (para encolar trabajos hijos)."""
        return getattr(self._local, "job_id", None)

    def enqueue(
        self,
        task: str,
        payload: dict[str, Any] | None = None,
        *,
        dedupe_key: str | None = None,
        idempotency_key: str | None = None,
        created_by: str | None = None,
        parent_id: int | None = None,
        run_after: datetime | None = None,
        publish: bool = True,
    ) -> EnqueueResult:
        spec = self.registry.get(task)
        row, created = self.repository.create(
            task=spec.name,
            queue=spec.queue,
            payload=payload,
            max_attempts=spec.max_attempts or self.default_max_attempts,
            dedupe_key=dedupe_key,
            idempotency_key=idempotency_key,
            parent_id=parent_id if parent_id is not None else self.current_job_id,
            created_by=created_by,
            run_after=run_after,
        )
        result = EnqueueResult(row, created)
        if created and publish and run_after is None:
            self.publish(row)
        return result

    def publish(self, job: dict[str, Any], *, delay_s: float = 0) -> None:
        """Envía el trabajo a la cola. Si falla, queda sin publicar y lo reenvía el programador."""
        try:
            sent = self.queue.publish(job, delay_s=delay_s)
        except Exception as error:
            self.logger.warning(
                "no se pudo publicar el trabajo #%s: %s", job["id"], error_message(error)
            )
            return
        if sent:
            self.repository.mark_published(int(job["id"]), attempts=int(job.get("attempts") or 0))

    def relay(self, *, now: datetime, grace_s: int = 60) -> int:
        """Publica los trabajos nuevos que quedaron sin enviar y los reintentos vencidos."""
        due = self.repository.due_unpublished(now=now, grace_s=grace_s)
        for job in due:
            self.publish(job)
        return len(due)

    def run_now(self, job_id: int) -> dict[str, Any] | None:
        """Ejecuta el trabajo en este proceso y devuelve su estado final."""
        self.execute(job_id)
        return self.repository.get(job_id)

    def execute(self, job_id: int, *, worker: str | None = None) -> str:
        """Ejecuta un trabajo. Devuelve succeeded | retrying | failed | skipped.

        Los errores de la tarea quedan en la tabla `jobs` (no se propagan); un error de
        infraestructura (p. ej. SQL Server caído al reclamarlo) sí, para que la cola lo
        reintente.
        """
        from invoicebot.infrastructure.db.job_repository import later

        job = self.repository.claim(job_id, worker or worker_name())
        if job is None:
            return "skipped"
        try:
            spec = self.registry.get(job["task"])
        except ConfigError as error:
            self.repository.fail(job_id, error.message, retry_at=None)
            return "failed"
        payload = job.get("payload") or {}
        previous = self.current_job_id
        self._local.job_id = job_id
        log_extra = {"job_id": job_id, "task": spec.name, "attempt": job["attempts"]}
        self.logger.info("inicio del trabajo %s #%s", spec.name, job_id, extra=log_extra)
        try:
            result = spec.handler(self.context, payload if isinstance(payload, dict) else {})
        except RetryLater as wait:
            self.repository.defer(job_id, later(wait.delay_s), wait.message)
            # defer devuelve el intento: el trabajo queda con uno menos.
            self.publish(
                {**job, "status": "retrying", "attempts": int(job["attempts"]) - 1},
                delay_s=wait.delay_s,
            )
            return "retrying"
        except Exception as error:
            message = error_message(error)
            permanent = isinstance(error, PermanentJobError | ConfigError)
            if permanent or job["attempts"] >= job["max_attempts"]:
                self.logger.error(
                    "trabajo %s #%s fallido: %s", spec.name, job_id, message, extra=log_extra
                )
                self.repository.fail(job_id, message, retry_at=None)
                return "failed"
            delay = backoff_seconds(int(job["attempts"]))
            self.logger.warning(
                "trabajo %s #%s fallido (se reintenta en %.0f s): %s",
                spec.name,
                job_id,
                delay,
                message,
                extra=log_extra,
            )
            self.repository.fail(job_id, message, retry_at=later(delay))
            self.publish({**job, "status": "retrying"}, delay_s=delay)
            return "retrying"
        finally:
            self._local.job_id = previous
        self.repository.succeed(job_id, _plain(result))
        self.logger.info("fin del trabajo %s #%s", spec.name, job_id, extra=log_extra)
        return "succeeded"


def _plain(value: Any) -> Any:
    """El resultado se guarda como JSON: dataclasses y objetos con as_dict() se convierten."""
    if value is None or isinstance(value, str | int | float | bool | list | dict):
        return value
    if hasattr(value, "as_dict"):
        return value.as_dict()
    if hasattr(value, "__dataclass_fields__"):
        from dataclasses import asdict

        return asdict(value)
    return str(value)
