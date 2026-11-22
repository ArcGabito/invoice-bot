"""Cola de trabajos sobre Redis con Dramatiq.

Cada cola de InvoiceBot (ingest, extract, sunat, default) tiene un actor genérico
que recibe el id de un trabajo y llama a ``Jobs.execute``. Los reintentos de las
tareas los decide InvoiceBot (tabla ``jobs``, espera exponencial); los de
Dramatiq solo cubren fallos de infraestructura (p. ej. SQL Server caído al
reclamar el trabajo).

Claves en Redis (``<namespace>`` = INVOICEBOT_QUEUE_NAMESPACE):

- ``<namespace>:<cola>``: lista de mensajes listos (la que mira el escalador KEDA).
- ``<namespace>:<cola>.XQ``: mensajes muertos (fallos de infraestructura agotados).

Las esperas (reintentos, límites de llamadas) no usan la cola de espera de Dramatiq:
las guarda SQL Server y las publica el programador.
"""

from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Callable, Sequence
from typing import Any

import dramatiq
from dramatiq.brokers.redis import RedisBroker
from dramatiq.middleware import (
    AgeLimit,
    Callbacks,
    CurrentMessage,
    Pipelines,
    Retries,
    ShutdownNotifications,
    TimeLimit,
)

from invoicebot.application.jobs import QUEUES, TaskRegistry

#: Reintentos de Dramatiq para fallos de infraestructura (no de la tarea).
INFRA_RETRIES = 5
#: Margen sobre el límite de la tarea más lenta de la cola.
TIME_LIMIT_MARGIN_S = 60


def actor_name(queue: str) -> str:
    return f"invoicebot_job_{queue}"


def create_redis_client(url: str) -> Any:
    import redis

    return redis.Redis.from_url(
        url, socket_timeout=10, socket_connect_timeout=5, health_check_interval=30
    )


class RedisQueue:
    backend = "redis"

    def __init__(
        self,
        *,
        client: Any,
        namespace: str,
        registry: TaskRegistry,
        execute: Callable[[int], Any] | None = None,
    ):
        self.client = client
        self.namespace = namespace
        self.registry = registry
        self.broker = RedisBroker(
            client=client,
            namespace=namespace,
            middleware=[
                AgeLimit(),
                TimeLimit(),
                ShutdownNotifications(),
                Callbacks(),
                Pipelines(),
                Retries(max_retries=INFRA_RETRIES, min_backoff=5_000, max_backoff=300_000),
                CurrentMessage(),
            ],
        )
        self._execute = execute
        self.actors: dict[str, dramatiq.Actor[Any, Any]] = {}
        for queue in QUEUES:
            limit = max(
                (s.time_limit_s for s in registry.tasks.values() if s.queue == queue),
                default=30 * 60,
            )
            self.actors[queue] = dramatiq.actor(
                actor_name=actor_name(queue),
                queue_name=queue,
                broker=self.broker,
                time_limit=(limit + TIME_LIMIT_MARGIN_S) * 1000,
            )(self._run)

    def bind(self, execute: Callable[[int], Any]) -> None:
        self._execute = execute

    def _run(self, job_id: int) -> None:
        if self._execute is None:
            raise RuntimeError("La cola no tiene ejecutor (usa RedisQueue.bind)")
        self._execute(int(job_id))

    def publish(self, job: dict[str, Any], *, delay_s: float = 0) -> bool:
        """Envía el id del trabajo a la lista de su cola.

        Un trabajo con espera no se envía: queda en SQL Server con `run_after` y lo
        publica el programador cuando vence. Así Redis solo tiene trabajos listos y los
        workers pueden escalar a cero (KEDA mira la longitud de la lista).
        """
        if delay_s > 0:
            return False
        self.actors[str(job["queue"])].send(int(job["id"]))
        return True

    def ping(self) -> str:
        self.client.ping()
        info = self.client.info("server")
        return f"Redis {info.get('redis_version', '?')}"

    def sizes(self) -> dict[str, dict[str, int]]:
        pipe = self.client.pipeline()
        for queue in QUEUES:
            base = f"{self.namespace}:{queue}"
            pipe.llen(base)
            pipe.zcard(f"{base}.XQ")
        values = pipe.execute()
        return {
            queue: {"ready": int(values[i * 2]), "dead": int(values[i * 2 + 1])}
            for i, queue in enumerate(QUEUES)
        }


def run_worker(
    queue: RedisQueue,
    *,
    queues: Sequence[str],
    threads: int,
    logger: logging.Logger,
    stop_event: threading.Event | None = None,
) -> None:
    """Atiende las colas hasta recibir SIGTERM o SIGINT (o ``stop_event``).

    Al detenerse deja terminar los trabajos en curso: Container Apps y Docker
    envían SIGTERM y esperan antes de matar el proceso.
    """
    worker = dramatiq.Worker(queue.broker, queues=set(queues), worker_threads=threads)
    stop = stop_event or threading.Event()

    def handle(signum: int, _frame: Any) -> None:
        logger.info("señal %s recibida: el worker termina los trabajos en curso", signum)
        stop.set()

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, handle)
        signal.signal(signal.SIGINT, handle)
    worker.start()
    logger.info("worker atendiendo %s con %s hilos", ", ".join(queues), threads)
    try:
        while not stop.wait(1):
            pass
    finally:
        worker.stop(timeout=60_000)
        logger.info("worker detenido")
