"""Envoltorio que pasa ciertas llamadas de un objeto por un límite de llamadas."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from invoicebot.infrastructure.queue import RateLimiter


class RateLimited:
    """``RateLimited(cliente, limiter, ("validate_receipt",))`` se comporta como ``cliente``,
    pero cada llamada a esos métodos espera un hueco del límite antes de hacerse."""

    def __init__(self, target: Any, limiter: RateLimiter, methods: Iterable[str]):
        self._target = target
        self._limiter = limiter
        self._methods = frozenset(methods)

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._target, name)
        if name not in self._methods or not callable(value):
            return value
        return self._wrap(value)

    def _wrap(self, method: Callable[..., Any]) -> Callable[..., Any]:
        def call(*args: Any, **kwargs: Any) -> Any:
            with self._limiter.slot():
                return method(*args, **kwargs)

        return call
