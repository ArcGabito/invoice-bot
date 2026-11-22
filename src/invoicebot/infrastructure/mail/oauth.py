"""Tokens OAuth 2.0 para Gmail y Microsoft Graph.

- Refresh token del usuario (`invoicebot mail login`), con rotación en Microsoft.
- Permiso de aplicación de Microsoft 365 con client secret o con certificado.
- Cuenta de servicio de Google Workspace con delegación de dominio (JWT RS256).
"""

from __future__ import annotations

import base64
import hashlib
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx
import jwt
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from invoicebot.infrastructure.mail.base import MailError


class TokenProvider(Protocol):
    def get_token(self) -> str: ...


def request_token(
    url: str, form: dict[str, str], *, client: httpx.Client | None = None, timeout_s: float = 30.0
) -> tuple[int, dict[str, Any]]:
    """POST de formulario a un endpoint de tokens."""
    http = client or httpx.Client(timeout=timeout_s)
    try:
        res = http.post(url, data=form, headers={"accept": "application/json"})
    except httpx.HTTPError as error:
        raise MailError(f"No se pudo contactar el servicio de inicio de sesión: {error}") from error
    try:
        body = res.json()
    except ValueError:
        body = {}
    return res.status_code, body if isinstance(body, dict) else {}


def token_error(status: int, body: dict[str, Any], what: str) -> MailError:
    """invalid_grant significa token vencido o revocado."""
    detail = (
        (body.get("error_description") or "").split("\r\n")[0]
        or body.get("error")
        or f"HTTP {status}"
    )
    hint = ""
    if body.get("error") == "invalid_grant":
        hint = (
            " El acceso venció o fue revocado: vuelve a iniciar sesión con `invoicebot mail login`."
        )
    elif body.get("error") in ("invalid_client", "unauthorized_client"):
        hint = " Revisa el client ID y el client secret."
    return MailError(f"{what}: {detail}.{hint}", auth=status in (400, 401))


class CachedToken:
    """Cachea el token hasta un minuto antes de que venza (seguro entre hilos)."""

    def __init__(self, fetch: Callable[[], tuple[str, int]]):
        self._fetch = fetch
        self._lock = threading.Lock()
        self._current: tuple[str, float] | None = None

    def get_token(self) -> str:
        with self._lock:
            if self._current and time.monotonic() < self._current[1]:
                return self._current[0]
            token, expires_in = self._fetch()
            self._current = (token, time.monotonic() + max(expires_in - 60, 30))
            return token


def refresh_token_provider(
    *,
    token_url: str,
    client_id: str,
    refresh_token: str,
    client_secret: str | None = None,
    scope: str | None = None,
    on_rotate: Callable[[str], None] | None = None,
    client: httpx.Client | None = None,
    timeout_s: float = 30.0,
) -> CachedToken:
    state = {"refresh": refresh_token}

    def fetch() -> tuple[str, int]:
        form = {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": state["refresh"],
        }
        if client_secret:
            form["client_secret"] = client_secret
        if scope:
            form["scope"] = scope
        status, body = request_token(token_url, form, client=client, timeout_s=timeout_s)
        if not body.get("access_token"):
            raise token_error(status, body, "No se pudo renovar el acceso al correo")
        rotated = body.get("refresh_token")
        if rotated and rotated != state["refresh"]:
            # Microsoft entrega uno nuevo en cada renovación: se guarda para que no venza.
            state["refresh"] = rotated
            if on_rotate:
                on_rotate(rotated)
        return str(body["access_token"]), int(body.get("expires_in") or 3600)

    return CachedToken(fetch)


def client_credentials_provider(
    *,
    token_url: str,
    client_id: str,
    client_secret: str,
    scope: str,
    client: httpx.Client | None = None,
    timeout_s: float = 30.0,
) -> CachedToken:
    def fetch() -> tuple[str, int]:
        status, body = request_token(
            token_url,
            {
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": scope,
            },
            client=client,
            timeout_s=timeout_s,
        )
        if not body.get("access_token"):
            raise token_error(status, body, "Microsoft rechazó las credenciales de la aplicación")
        return str(body["access_token"]), int(body.get("expires_in") or 3600)

    return CachedToken(fetch)


def google_service_account_provider(
    *,
    key: dict[str, Any],
    subject: str,
    scope: str,
    client: httpx.Client | None = None,
    timeout_s: float = 30.0,
) -> CachedToken:
    """Firma un JWT (RS256) a nombre del buzón `subject` (delegación de todo el dominio)."""
    token_url = key.get("token_uri") or "https://oauth2.googleapis.com/token"

    def fetch() -> tuple[str, int]:
        now = int(time.time())
        claims = {
            "iss": key["client_email"],
            "sub": subject,
            "scope": scope,
            "aud": token_url,
            "iat": now,
            "exp": now + 3600,
        }
        try:
            assertion = jwt.encode(claims, key["private_key"], algorithm="RS256")
        except (ValueError, TypeError, KeyError) as error:
            raise MailError(
                f"La clave de la cuenta de servicio no es válida: {error}", auth=True
            ) from error
        status, body = request_token(
            token_url,
            {"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion},
            client=client,
            timeout_s=timeout_s,
        )
        if not body.get("access_token"):
            raise token_error(
                status,
                body,
                "Google rechazó la cuenta de servicio (¿tiene delegación de dominio con el permiso "
                "gmail.readonly?)",
            )
        return str(body["access_token"]), int(body.get("expires_in") or 3600)

    return CachedToken(fetch)


@dataclass(frozen=True)
class AppCertificate:
    thumbprint_s256: str
    private_key: RSAPrivateKey


def read_certificate_pem(pem: bytes, passphrase: str | None = None) -> AppCertificate:
    """PEM con certificado y clave privada (p. ej. `openssl pkcs12 -in app.pfx -out app.pem -nodes`)."""
    try:
        cert = x509.load_pem_x509_certificate(pem)
    except ValueError as error:
        raise MailError("El archivo no contiene un certificado (BEGIN CERTIFICATE)") from error
    try:
        key = serialization.load_pem_private_key(
            pem, password=passphrase.encode() if passphrase else None
        )
    except (ValueError, TypeError) as error:
        raise MailError(
            f"No se pudo leer la clave privada del certificado: {error}. El PEM debe incluir la "
            "clave (si está cifrada, define INVOICEBOT_MAIL_MS_CERT_PASSPHRASE)."
        ) from error
    if not isinstance(key, RSAPrivateKey):
        raise MailError("La clave del certificado debe ser RSA")
    if key.public_key().public_numbers() != cert.public_key().public_numbers():  # type: ignore[union-attr]
        raise MailError("La clave privada no corresponde al certificado")
    if cert.not_valid_after_utc < datetime.now(UTC):
        raise MailError(
            f"El certificado venció el {cert.not_valid_after_utc:%Y-%m-%d}: genera uno nuevo y "
            "súbelo a la aplicación"
        )
    der = cert.public_bytes(serialization.Encoding.DER)
    thumbprint = base64.urlsafe_b64encode(hashlib.sha256(der).digest()).rstrip(b"=").decode()
    return AppCertificate(thumbprint, key)


def client_certificate_provider(
    *,
    token_url: str,
    client_id: str,
    certificate: AppCertificate,
    scope: str,
    client: httpx.Client | None = None,
    timeout_s: float = 30.0,
) -> CachedToken:
    """La aplicación se identifica con un JWT firmado con su clave (client assertion)."""

    def fetch() -> tuple[str, int]:
        now = int(time.time())
        assertion = jwt.encode(
            {
                "aud": token_url,
                "iss": client_id,
                "sub": client_id,
                "jti": str(uuid.uuid4()),
                "nbf": now,
                "iat": now,
                "exp": now + 600,
            },
            certificate.private_key,
            algorithm="RS256",
            headers={"x5t#S256": certificate.thumbprint_s256},
        )
        status, body = request_token(
            token_url,
            {
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                "client_assertion": assertion,
                "scope": scope,
            },
            client=client,
            timeout_s=timeout_s,
        )
        if not body.get("access_token"):
            raise token_error(status, body, "Microsoft rechazó el certificado de la aplicación")
        return str(body["access_token"]), int(body.get("expires_in") or 3600)

    return CachedToken(fetch)
