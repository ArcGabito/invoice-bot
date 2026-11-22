"""Autenticación de la API y de la pantalla de revisión.

- Personas: usuario y contraseña (scrypt) → cookie de sesión HttpOnly, SameSite=Strict
  (y Secure con HTTPS), con token antifalsificación (X-CSRF-Token) en cada acción.
  La sesión caduca a las INVOICEBOT_SERVE_SESSION_HOURS y se invalida al desactivar
  al usuario o cambiarle la contraseña.
- Sistemas (ERP, RPA, email-bot): clave de API `ibk_…` en `Authorization: Bearer` o
  `X-API-Key`, con alcance read o write.
- Bloqueo de 15 minutos tras 5 intentos fallidos por IP y por usuario.
"""

from __future__ import annotations

import hmac
import threading
import time
from dataclasses import dataclass

from fastapi import HTTPException, Request

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
    #: read | write
    scope: str
    csrf: str | None = None
    session_token: str | None = None

    @property
    def signature(self) -> str:
        """Quién firma las acciones de revisión."""
        return self.name if self.kind == "user" else f"api:{self.name}"


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


def authenticate_request(request: Request) -> Principal:
    """Dependencia de FastAPI: quién llama (o 401)."""
    ctx = request.app.state.context
    key = api_key_from(request)
    if key:
        found = ctx.users.find_api_key(key)
        if not found:
            raise HTTPException(401, "Clave de API no válida o revocada")
        return Principal("api_key", found["name"], found["name"], found["scope"])
    secure = is_secure_request(request, ctx.config.serve.cookie_secure)
    token = request.cookies.get(cookie_name(secure)) or request.cookies.get(cookie_name(not secure))
    session = ctx.users.get_session(token)
    if not session:
        raise HTTPException(401, "Inicia sesión para continuar")
    principal = Principal(
        "user",
        session["username"],
        session["display_name"],
        "write",
        csrf=session["csrf_token"],
        session_token=token,
    )
    if request.method not in SAFE_METHODS and not safe_equal(
        request.headers.get("x-csrf-token"), principal.csrf
    ):
        raise HTTPException(403, "Token de sesión no válido: recarga la página")
    return principal


def require_write(principal: Principal) -> Principal:
    if principal.scope != "write":
        raise HTTPException(403, "Esta clave de API es de solo lectura")
    return principal
