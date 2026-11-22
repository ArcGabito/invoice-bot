"""Autenticación de la API y de la pantalla de revisión.

- Personas: usuario y contraseña (scrypt) → cookie de sesión HttpOnly, SameSite=Strict
  (y Secure con HTTPS), con token antifalsificación (X-CSRF-Token) en cada acción.
  La sesión caduca a las INVOICEBOT_SERVE_SESSION_HOURS y se invalida al desactivar
  al usuario o cambiarle la contraseña.
- Sistemas (ERP, RPA, email-bot): clave de API `ibk_…` en `Authorization: Bearer` o
  `X-API-Key`, con alcance read o write o permisos finos, y opcionalmente una empresa.
- Cada persona tiene un rol (invoicebot.domain.access) y puede limitarse a ciertas
  empresas: la API fija ese alcance en la conexión y SQL Server filtra los comprobantes
  (Row-Level Security).
- Bloqueo de 15 minutos tras 5 intentos fallidos por IP y por usuario.
"""

from __future__ import annotations

import hmac
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from fastapi import HTTPException, Request

from invoicebot.domain.access import api_key_permissions, role_permissions
from invoicebot.infrastructure.db.connection import ACTOR, COMPANY_SCOPE

#: Intentos fallidos antes del bloqueo, y duración del bloqueo.
MAX_FAILURES = 5
LOCK_SECONDS = 15 * 60
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass(frozen=True)
class Principal:
    #: user | api_key
    kind: str
    name: str
    display_name: str
    #: read | write (compatibilidad con la 1.0: write si puede modificar algo)
    scope: str
    csrf: str | None = None
    session_token: str | None = None
    role: str | None = None
    permissions: frozenset[str] = field(default_factory=frozenset)
    #: None = todas las empresas.
    company_ids: tuple[int, ...] | None = None
    user_id: int | None = None

    @property
    def signature(self) -> str:
        """Quién firma las acciones de revisión."""
        return self.name if self.kind == "user" else f"api:{self.name}"

    def can(self, permission: str) -> bool:
        return permission in self.permissions


class LoginThrottle:
    def __init__(self, max_failures: int = MAX_FAILURES, lock_seconds: int = LOCK_SECONDS):
        self.max_failures = max_failures
        self.lock_seconds = lock_seconds
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> list[float]:
        return [t for t in self._failures.get(key, []) if now - t < self.lock_seconds]

    def is_locked(self, keys: list[str]) -> bool:
        now = time.monotonic()
        with self._lock:
            return any(len(self._recent(k, now)) >= self.max_failures for k in keys)

    def record_failure(self, keys: list[str]) -> None:
        now = time.monotonic()
        with self._lock:
            for key in keys:
                self._failures[key] = [*self._recent(key, now), now]

    def clear(self, keys: list[str]) -> None:
        with self._lock:
            for key in keys:
                self._failures.pop(key, None)


def cookie_name(secure: bool) -> str:
    # El prefijo __Host- obliga al navegador a aceptarla solo por HTTPS y para este servidor.
    return "__Host-invoicebot" if secure else "invoicebot_session"


def is_secure_request(request: Request, setting: str) -> bool:
    if setting == "true":
        return True
    if setting == "false":
        return False
    return request.url.scheme == "https"


def safe_equal(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(str(a), str(b))


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "desconocida"


def api_key_from(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.headers.get("x-api-key")


_READ_ONLY = frozenset({"invoices:read", "exports:read", "jobs:read", "audit:read"})


def authenticate_request(request: Request) -> Principal:
    """Quién llama (o 401). Sin efectos: el alcance de empresas lo fija `principal_dependency`."""
    ctx = request.app.state.context
    key = api_key_from(request)
    if key:
        found = ctx.users.find_api_key(key)
        if not found:
            raise HTTPException(401, "Clave de API no válida o revocada")
        permissions = api_key_permissions(found["scope"], found.get("permissions"))
        company = found.get("company_id")
        return Principal(
            "api_key",
            found["name"],
            found["name"],
            "write" if permissions - _READ_ONLY else "read",
            permissions=permissions,
            company_ids=(int(company),) if company is not None else None,
        )
    secure = is_secure_request(request, ctx.config.serve.cookie_secure)
    token = request.cookies.get(cookie_name(secure)) or request.cookies.get(cookie_name(not secure))
    session = ctx.users.get_session(token)
    if not session:
        raise HTTPException(401, "Inicia sesión para continuar")
    role = session.get("role") or "admin"
    permissions = role_permissions(role)
    companies = (
        None
        if session.get("all_companies", True)
        else tuple(ctx.users.company_ids(session["user_id"]))
    )
    principal = Principal(
        "user",
        session["username"],
        session["display_name"],
        "write" if permissions - _READ_ONLY else "read",
        csrf=session["csrf_token"],
        session_token=token,
        role=role,
        permissions=permissions,
        company_ids=companies,
        user_id=session["user_id"],
    )
    if request.method not in SAFE_METHODS and not safe_equal(
        request.headers.get("x-csrf-token"), principal.csrf
    ):
        raise HTTPException(403, "Token de sesión no válido: recarga la página")
    return principal


async def principal_dependency(request: Request) -> Principal:
    """Dependencia de FastAPI: autentica y fija el alcance de empresas de la petición.

    Es asíncrona a propósito: la variable de contexto que fija aquí la heredan los
    hilos donde FastAPI ejecuta los endpoints síncronos (y sus consultas a SQL Server).
    """
    from starlette.concurrency import run_in_threadpool

    principal = await run_in_threadpool(authenticate_request, request)
    COMPANY_SCOPE.set(principal.company_ids)
    ACTOR.set(principal.signature)
    return principal


def require(permission: str) -> Callable[[Principal], Principal]:
    def check(principal: Principal) -> Principal:
        if not principal.can(permission):
            who = "Esta clave de API" if principal.kind == "api_key" else "Tu rol"
            raise HTTPException(403, f"{who} no tiene el permiso {permission}")
        return principal

    return check


def require_write(principal: Principal) -> Principal:
    if principal.scope != "write":
        raise HTTPException(403, "Esta clave de API es de solo lectura")
    return principal
