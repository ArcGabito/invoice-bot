"""Transporte de trabajos: en el mismo proceso (inline) o Dramatiq + Redis."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from invoicebot.errors import InvoiceBotError


class InlineQueue:
    """Ejecuta cada trabajo en el proceso que lo publica, al momento.

    Un trabajo con espera (reintento) no se ejecuta aquí: queda sin publicar y lo
    ejecuta el próximo `invoicebot schedule tick` o `invoicebot scheduler` (igual que
    con Redis).
    """

    backend = "inline"

    def __init__(self, execute: Callable[[int], Any]):
        self._execute = execute

    def publish(self, job: dict[str, Any], *, delay_s: float = 0) -> bool:
        if delay_s > 0:
            return False
        self._execute(int(job["id"]))
        return True

    def ping(self) -> str:
        return "en el mismo proceso (sin Redis)"

    def sizes(self) -> dict[str, dict[str, int]]:
        return {}


class RateLimiter:
    """Límite de llamadas por minuto compartido entre procesos (o local, sin Redis)."""

    def __init__(self, key: str, per_minute: int, *, redis_client: Any | None = None):
        self.key = key
        self.per_minute = per_minute
        self._limiter: Any = None
        self._local: list[float] = []
        self._lock = threading.Lock()
        if per_minute > 0 and redis_client is not None:
            from dramatiq.rate_limits import WindowRateLimiter
            from dramatiq.rate_limits.backends import RedisBackend

            self._limiter = WindowRateLimiter(
                RedisBackend(client=redis_client), key, limit=per_minute, window=60
            )

    def _try_local(self) -> bool:
        now = time.monotonic()
        with self._lock:
            self._local = [t for t in self._local if now - t < 60]
            if len(self._local) < self.per_minute:
                self._local.append(now)
                return True
            return False

    def try_acquire(self) -> bool:
        """Toma un hueco sin esperar (False si no hay)."""
        if self.per_minute <= 0:
            return True
        return self._try()

    def _try(self) -> bool:
        if self._limiter is None:
            return self._try_local()
        with self._limiter.acquire(raise_on_failure=False) as acquired:
            return bool(acquired)

    @contextmanager
    def slot(self, *, timeout_s: float = 300) -> Iterator[None]:
        """Espera un hueco (hasta ``timeout_s``). Si no llega, RateLimitTimeout."""
        if self.per_minute <= 0:
            yield
            return
        deadline = time.monotonic() + timeout_s
        while not self._try():
            if time.monotonic() >= deadline:
                raise RateLimitTimeout(
                    f"Límite de {self.per_minute} llamadas por minuto ({self.key}) sin hueco"
                )
            time.sleep(1)
        yield


class RateLimitTimeout(InvoiceBotError):
    code = "RATE_LIMIT_TIMEOUT"
