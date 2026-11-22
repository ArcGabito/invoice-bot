"""Inicio de sesión del usuario en el buzón (`invoicebot mail login`).

- Google: código de autorización con PKCE y redirección a 127.0.0.1 (app de escritorio).
- Microsoft: flujo de código de dispositivo (microsoft.com/devicelogin).

El acceso se guarda en data/mail-token.json: protégelo como una contraseña.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import tempfile
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from invoicebot.infrastructure.mail.base import MailError
from invoicebot.infrastructure.mail.oauth import request_token
from invoicebot.infrastructure.mail.sources import (
    GMAIL_SCOPE,
    GRAPH_DELEGATED_SCOPE,
    MICROSOFT_LOGIN_URL,
)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_LOGIN_TIMEOUT_S = 300


def _page(text: str) -> bytes:
    return (
        '<!doctype html><html lang="es"><meta charset="utf-8"><title>InvoiceBot</title>'
        '<body style="font-family:system-ui;max-width:32rem;margin:4rem auto">'
        f"<h1>InvoiceBot</h1><p>{text}</p></body></html>"
    ).encode()


def google_login(
    *,
    client_id: str,
    client_secret: str,
    token_url: str = GOOGLE_TOKEN_URL,
    auth_url: str = GOOGLE_AUTH_URL,
    scope: str = GMAIL_SCOPE,
    open_browser: Callable[[str], object],
    say: Callable[[str], None],
    timeout_s: float = _LOGIN_TIMEOUT_S,
) -> str:
    """Devuelve el refresh token."""
    verifier = secrets.token_urlsafe(32)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    state = secrets.token_urlsafe(16)
    outcome: dict[str, str] = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            url = urlparse(self.path)
            if url.path != "/":
                self.send_response(404)
                self.end_headers()
                return
            params = {k: v[0] for k, v in parse_qs(url.query).items()}
            ok = "code" in params and "error" not in params and params.get("state") == state
            self.send_response(200 if ok else 400)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                _page("Listo: ya puedes cerrar esta pestaña y volver a la consola.")
                if ok
                else _page(
                    f"No se completó el inicio de sesión ({params.get('error', 'respuesta no válida')})."
                )
            )
            if ok:
                outcome["code"] = params["code"]
            else:
                outcome["error"] = params.get("error", "respuesta no válida")
            done.set()

        def log_message(self, *_args: Any) -> None:
            return None

    server = HTTPServer(("127.0.0.1", 0), Handler)
    redirect_uri = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"{auth_url}?" + urlencode(
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": scope,
                "access_type": "offline",
                "prompt": "consent",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": state,
            }
        )
        say("Abre esta dirección en el navegador e inicia sesión con la cuenta de Gmail:")
        say(url)
        open_browser(url)
        if not done.wait(timeout_s):
            raise MailError("Inicio de sesión cancelado: tiempo de espera agotado", auth=True)
        if "error" in outcome:
            raise MailError(f"Inicio de sesión cancelado: {outcome['error']}", auth=True)
        status, body = request_token(
            token_url,
            {
                "grant_type": "authorization_code",
                "code": outcome["code"],
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
            },
        )
        if not body.get("refresh_token"):
            detail = body.get("error_description") or body.get("error") or f"HTTP {status}"
            raise MailError(f"Google no entregó el acceso: {detail}", auth=True)
        return str(body["refresh_token"])
    finally:
        server.shutdown()
        server.server_close()


def microsoft_device_login(
    *,
    client_id: str,
    tenant: str,
    login_url: str = MICROSOFT_LOGIN_URL,
    scope: str = GRAPH_DELEGATED_SCOPE,
    say: Callable[[str], None],
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    base = f"{login_url}/{tenant}/oauth2/v2.0"
    http = client or httpx.Client(timeout=30)
    try:
        res = http.post(f"{base}/devicecode", data={"client_id": client_id, "scope": scope})
        device = res.json()
    except (httpx.HTTPError, ValueError) as error:
        raise MailError(f"No se pudo contactar a Microsoft: {error}") from error
    if not device.get("device_code") or not device.get("user_code"):
        detail = (device.get("error_description") or "").split("\r\n")[
            0
        ] or f"HTTP {res.status_code}"
        raise MailError(
            f"Microsoft rechazó el inicio de sesión: {detail}. Revisa INVOICEBOT_MAIL_MS_CLIENT_ID, "
            'INVOICEBOT_MAIL_MS_TENANT y que la aplicación permita "flujos de cliente público".',
            auth=True,
        )
    say(
        f"Abre {device.get('verification_uri', 'https://microsoft.com/devicelogin')} en un navegador, "
        f"escribe el código {device['user_code']} e inicia sesión con la cuenta de correo."
    )
    interval = float(device.get("interval") or 5)
    deadline = time.monotonic() + float(device.get("expires_in") or 900)
    while time.monotonic() < deadline:
        sleep(interval)
        status, body = request_token(
            f"{base}/token",
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": client_id,
                "device_code": device["device_code"],
            },
            client=http,
        )
        if body.get("refresh_token"):
            return str(body["refresh_token"])
        if body.get("error") == "authorization_pending":
            continue
        if body.get("error") == "slow_down":
            interval += 5
            continue
        detail = (
            (body.get("error_description") or "").split("\r\n")[0]
            or body.get("error")
            or f"HTTP {status}"
        )
        raise MailError(f"No se completó el inicio de sesión: {detail}", auth=True)
    raise MailError("El código venció sin que se iniciara sesión", auth=True)


# --- Almacén del acceso ------------------------------------------------------------


def read_mail_token(path: str) -> dict[str, Any] | None:
    try:
        token = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return token if isinstance(token, dict) and token.get("refreshToken") else None


def write_mail_token(
    path: str, *, provider: str, client_id: str, account: str, refresh_token: str
) -> None:
    """Escritura atómica con permisos solo para el propietario."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "provider": provider,
        "clientId": client_id,
        "account": account,
        "refreshToken": refresh_token,
        "savedAt": datetime.now(UTC).isoformat(),
    }
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".mail-token-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
