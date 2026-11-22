"""Peticiones autenticadas a las API de correo (Gmail, Graph)."""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from invoicebot.infrastructure.mail.base import MailError
from invoicebot.infrastructure.mail.oauth import TokenProvider

#: Espera máxima que se respeta de un Retry-After antes de reintentar.
_MAX_RETRY_WAIT_S = 10.0


class MailApi:
    def __init__(
        self,
        *,
        tokens: TokenProvider,
        service: str,
        client: httpx.Client | None = None,
        timeout_s: float = 60.0,
    ):
        self.tokens = tokens
        self.service = service
        self.http = client or httpx.Client(timeout=timeout_s)

    def request(self, url: str, *, body: Any = None, method: str | None = None) -> httpx.Response:
        """GET, o POST con `body` en JSON. Reintenta una vez si el servicio pide esperar."""
        verb = method or ("GET" if body is None else "POST")
        for attempt in (1, 2):
            headers = {"authorization": f"Bearer {self.tokens.get_token()}"}
            try:
                res = self.http.request(verb, url, headers=headers, json=body)
            except httpx.HTTPError as error:
                raise MailError(f"No se pudo contactar {self.service}: {error}") from error
            if res.status_code < 400:
                return res
            if res.status_code in (429, 503) and attempt == 1:
                try:
                    wait = float(res.headers.get("retry-after", "2"))
                except ValueError:
                    wait = 2.0
                time.sleep(min(wait, _MAX_RETRY_WAIT_S))
                continue
            detail = res.text[:300]
            try:
                parsed = res.json()
                problem = parsed.get("error") if isinstance(parsed, dict) else None
                detail = (
                    problem if isinstance(problem, str) else (problem or {}).get("message", detail)
                )
            except ValueError:
                pass
            auth = res.status_code in (401, 403)
            hint = " (revisa los permisos de la aplicación y la cuenta)" if auth else ""
            raise MailError(
                f"{self.service} respondió {res.status_code}: {detail}{hint}", auth=auth
            )
        raise MailError(f"{self.service} no respondió")  # pragma: no cover

    def json(self, url: str, *, body: Any = None) -> dict[str, Any]:
        text = self.request(url, body=body).text
        return json.loads(text) if text else {}
