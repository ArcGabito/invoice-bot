"""Inicio de sesión con Microsoft Entra ID, Google Workspace u otro proveedor OpenID Connect.

Flujo de código de autorización con PKCE (S256), ``state`` de un solo uso guardado en
SQL Server (sirve con varias réplicas de la API) y ``nonce`` en el ``id_token``. El
token se verifica con las claves públicas del proveedor (JWKS): firma asimétrica,
emisor, audiencia, vigencia y nonce. Nunca se aceptan ``none`` ni HS256.

Ejemplos de emisor (INVOICEBOT_OIDC_ISSUER):
- Microsoft Entra ID: ``https://login.microsoftonline.com/<id-del-directorio>/v2.0``
- Google: ``https://accounts.google.com``
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt

from invoicebot.errors import InvoiceBotError

ALLOWED_ALGORITHMS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384")
_CACHE_SECONDS = 3600


class OidcError(InvoiceBotError):
    code = "OIDC_ERROR"


@dataclass(frozen=True)
class OidcIdentity:
    subject: str
    email: str | None
    name: str | None
    email_verified: bool


def pkce_pair() -> tuple[str, str]:
    """(code_verifier, code_challenge S256)."""
    verifier = secrets.token_urlsafe(64)[:96]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


class OidcClient:
    def __init__(
        self,
        *,
        issuer: str,
        client_id: str,
        client_secret: str | None,
        scopes: str = "openid email profile",
        http: httpx.Client | None = None,
        timeout_s: float = 10,
    ):
        self.issuer = issuer.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.scopes = scopes
        self.http = http or httpx.Client(timeout=timeout_s, follow_redirects=False)
        self._lock = threading.Lock()
        self._metadata: tuple[float, dict[str, Any]] | None = None
        self._jwks: tuple[float, dict[str, Any]] | None = None

    def _get_json(self, url: str) -> dict[str, Any]:
        try:
            response = self.http.get(url)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise OidcError(f"No se pudo leer {url}: {error}") from error
        if not isinstance(data, dict):
            raise OidcError(f"Respuesta inesperada de {url}")
        return data

    def metadata(self) -> dict[str, Any]:
        with self._lock:
            if self._metadata and time.monotonic() - self._metadata[0] < _CACHE_SECONDS:
                return self._metadata[1]
        data = self._get_json(f"{self.issuer}/.well-known/openid-configuration")
        if str(data.get("issuer", "")).rstrip("/") != self.issuer:
            raise OidcError(
                f"El emisor anunciado ({data.get('issuer')}) no coincide con INVOICEBOT_OIDC_ISSUER "
                "(en Microsoft usa el ID del directorio, no common)"
            )
        for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            if not str(data.get(key, "")).startswith("https://"):
                raise OidcError(f"Falta {key} (https) en la configuración del proveedor")
        with self._lock:
            self._metadata = (time.monotonic(), data)
        return data

    def _keys(self, *, refresh: bool = False) -> dict[str, Any]:
        with self._lock:
            if not refresh and self._jwks and time.monotonic() - self._jwks[0] < _CACHE_SECONDS:
                return self._jwks[1]
        data = self._get_json(self.metadata()["jwks_uri"])
        with self._lock:
            self._jwks = (time.monotonic(), data)
        return data

    def authorization_url(
        self, *, redirect_uri: str, state: str, nonce: str, code_challenge: str
    ) -> str:
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "scope": self.scopes,
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "prompt": "select_account",
        }
        return f"{self.metadata()['authorization_endpoint']}?{urlencode(params)}"

    def exchange(self, *, code: str, redirect_uri: str, code_verifier: str) -> dict[str, Any]:
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": self.client_id,
            "code_verifier": code_verifier,
        }
        if self.client_secret:
            form["client_secret"] = self.client_secret
        try:
            response = self.http.post(
                self.metadata()["token_endpoint"],
                data=form,
                headers={"accept": "application/json"},
            )
            data = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise OidcError(f"No se pudo canjear el código: {error}") from error
        if response.status_code != 200 or "id_token" not in data:
            detail = data.get("error_description") or data.get("error") or response.status_code
            raise OidcError(f"El proveedor rechazó el código: {detail}")
        return data

    def verify_id_token(self, id_token: str, *, nonce: str) -> OidcIdentity:
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.PyJWTError as error:
            raise OidcError(f"id_token no válido: {error}") from error
        algorithm = header.get("alg")
        if algorithm not in ALLOWED_ALGORITHMS:
            raise OidcError(f"Algoritmo de firma no permitido: {algorithm}")
        key = self._find_key(header.get("kid"))
        try:
            claims = jwt.decode(
                id_token,
                key=key,
                algorithms=[algorithm],
                audience=self.client_id,
                issuer=self.metadata()["issuer"],
                leeway=60,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as error:
            raise OidcError(f"id_token no válido: {error}") from error
        if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
            raise OidcError("El nonce del id_token no coincide")
        email = claims.get("email") or claims.get("preferred_username") or claims.get("upn")
        verified = claims.get("email_verified")
        return OidcIdentity(
            subject=str(claims["sub"]),
            email=str(email).strip().lower() if email else None,
            name=claims.get("name"),
            # Microsoft no envía email_verified: sus cuentas de directorio son del tenant.
            email_verified=bool(verified) if verified is not None else "@" in str(email or ""),
        )

    def _find_key(self, kid: str | None) -> Any:
        for refresh in (False, True):
            jwks = self._keys(refresh=refresh)
            try:
                keyset = jwt.PyJWKSet.from_dict(jwks)
            except jwt.PyJWTError as error:
                raise OidcError(f"JWKS del proveedor no válido: {error}") from error
            for key in keyset.keys:
                if kid is None or key.key_id == kid:
                    return key.key
        raise OidcError("El id_token está firmado con una clave desconocida")
