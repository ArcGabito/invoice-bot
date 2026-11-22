"""Servicios de SUNAT: Consulta Integrada de Comprobante de Pago y tipo de cambio.

Consulta integrada (Manual de Consulta Integrada por Servicio Web v2.0): una sola
consulta devuelve el estado del comprobante, el del contribuyente emisor y su
condición de domicilio. Requiere credenciales de API SUNAT (client_id y
client_secret) generadas con la Clave SOL de la empresa que consulta.

Tipo de cambio: archivo público del día, sin credenciales:
"31/10/2026|3.458|3.467|" (fecha|compra|venta).
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from datetime import date
from typing import Any
from urllib.parse import quote

import httpx

from invoicebot.config import Config
from invoicebot.domain.sunat_status import SunatResult
from invoicebot.errors import ConfigError, InvoiceBotError
from invoicebot.money import format_cents

SUNAT_AUTH_URL = "https://api-seguridad.sunat.gob.pe/v1/clientesextranet"
SUNAT_API_URL = "https://api.sunat.gob.pe/v1/contribuyente/contribuyentes"
SUNAT_EXCHANGE_RATE_URL = "https://www.sunat.gob.pe/a/txt/tipoCambio.txt"
_SCOPE = "https://api.sunat.gob.pe/v1/contribuyente/contribuyentes"

#: Tipos de comprobante (codComp) que admite el servicio.
SUPPORTED_DOCUMENT_TYPES = ("01", "03", "04", "07", "08", "R1", "R7")
_TOKEN_SAFETY_MARGIN_S = 60


class SunatError(InvoiceBotError):
    code = "SUNAT_ERROR"

    def __init__(self, message: str, *, kind: str = "unknown", retryable: bool = False):
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable


class SunatClient:
    def __init__(
        self,
        *,
        ruc: str,
        client_id: str,
        client_secret: str,
        auth_url: str = SUNAT_AUTH_URL,
        api_url: str = SUNAT_API_URL,
        timeout_s: float = 20.0,
        client: httpx.Client | None = None,
        now: Callable[[], float] = time.monotonic,
    ):
        self.ruc = ruc
        self.client_id = client_id
        self.client_secret = client_secret
        self.auth_url = auth_url.rstrip("/")
        self.api_url = api_url.rstrip("/")
        self.http = client or httpx.Client(timeout=timeout_s)
        self.now = now
        self._token: tuple[str, float] | None = None

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            return self.http.request(method, url, **kwargs)
        except httpx.HTTPError as error:
            raise SunatError(
                f"No se pudo conectar con SUNAT: {error}", kind="transient", retryable=True
            ) from error

    @staticmethod
    def _json(res: httpx.Response) -> Any:
        try:
            return res.json()
        except ValueError:
            return None

    def get_token(self, *, refresh: bool = False) -> str:
        if not refresh and self._token and self._token[1] > self.now():
            return self._token[0]
        res = self._request(
            "POST",
            f"{self.auth_url}/{quote(self.client_id, safe='')}/oauth2/token/",
            data={
                "grant_type": "client_credentials",
                "scope": _SCOPE,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
        )
        if res.status_code in (400, 401):
            raise SunatError(
                "SUNAT rechazó las credenciales de API (client_id o client_secret)",
                kind="sunat_auth",
            )
        body = self._json(res) or {}
        token = body.get("access_token") if isinstance(body, dict) else None
        if not token:
            transient = res.status_code >= 500
            raise SunatError(
                f"SUNAT no devolvió un token (HTTP {res.status_code})",
                kind="transient" if transient else "sunat_auth",
                retryable=transient,
            )
        ttl = float(body.get("expires_in") or 3600)
        self._token = (token, self.now() + ttl - _TOKEN_SAFETY_MARGIN_S)
        return str(token)

    def validate_receipt(
        self,
        *,
        supplier_ruc: str,
        document_type: str,
        series: str,
        number: str,
        issue_date: str,
        total_cents: int | None,
    ) -> SunatResult:
        """`total_cents` solo se envía en comprobantes electrónicos (serie que empieza con letra)."""
        year, month, day = str(issue_date).split("-")
        body: dict[str, Any] = {
            "numRuc": supplier_ruc,
            "codComp": document_type,
            "numeroSerie": series,
            "numero": int(number),
            "fechaEmision": f"{day}/{month}/{year}",
        }
        if re.match(r"^[A-Z]", series, re.IGNORECASE) and total_cents is not None:
            body["monto"] = float(format_cents(total_cents))
        url = f"{self.api_url}/{self.ruc}/validarcomprobante"

        def send(token: str) -> httpx.Response:
            return self._request(
                "POST", url, json=body, headers={"authorization": f"Bearer {token}"}
            )

        res = send(self.get_token())
        if res.status_code == 401:
            res = send(self.get_token(refresh=True))
        if res.status_code == 429 or res.status_code >= 500:
            raise SunatError(
                f"Servicio de SUNAT no disponible (HTTP {res.status_code})",
                kind="transient",
                retryable=True,
            )
        payload = self._json(res)
        if not isinstance(payload, dict):
            payload = {}
        if res.status_code >= 400 or payload.get("success") is False:
            detail = ": ".join(
                str(v) for v in (payload.get("errorCode"), payload.get("message")) if v
            )
            raise SunatError(
                f"SUNAT rechazó la consulta ({detail or f'HTTP {res.status_code}'})",
                kind="sunat_auth" if res.status_code in (401, 403) else "sunat_rejected",
            )
        data = payload.get("data") or {}
        observations = data.get("observaciones") or data.get("Observaciones") or []
        estado_cp = data.get("estadoCp")
        return SunatResult(
            estado_cp=None if estado_cp is None else str(estado_cp),
            estado_ruc=data.get("estadoRuc"),
            cond_domi_ruc=data.get("condDomiRuc"),
            observaciones=[str(o) for o in observations] if isinstance(observations, list) else [],
            raw=payload,
        )

    def ping(self) -> str:
        self.get_token(refresh=True)
        return f"credenciales válidas para el RUC {self.ruc}"


def create_sunat_client(config: Config) -> SunatClient:
    """La verificación es opcional y debe activarse explícitamente."""
    s = config.sunat
    if not s.enabled:
        raise ConfigError(
            "La verificación en SUNAT está desactivada. Para usarla, define "
            "INVOICEBOT_SUNAT_ENABLED=true y las credenciales de API SUNAT en .env."
        )
    missing = [
        name
        for name, ok in (
            ("INVOICEBOT_SUNAT_RUC", s.ruc),
            ("INVOICEBOT_SUNAT_CLIENT_ID", s.client_id),
            ("INVOICEBOT_SUNAT_CLIENT_SECRET", s.client_secret_set),
        )
        if not ok
    ]
    if missing or not s.ruc or not s.client_id:
        raise ConfigError(
            f"Faltan credenciales de API SUNAT: {', '.join(missing)}. Se generan en SUNAT "
            "Operaciones en Línea → Credenciales de API SUNAT."
        )
    return SunatClient(
        ruc=s.ruc,
        client_id=s.client_id,
        client_secret=config.secret("INVOICEBOT_SUNAT_CLIENT_SECRET") or "",
        auth_url=s.auth_url or SUNAT_AUTH_URL,
        api_url=s.api_url or SUNAT_API_URL,
        timeout_s=s.timeout_s,
    )


# --- Tipo de cambio ----------------------------------------------------------------


class ExchangeRateError(InvoiceBotError):
    code = "EXCHANGE_RATE_ERROR"


def parse_sunat_exchange_rate(body: str) -> dict[str, Any]:
    m = re.fullmatch(r"\s*(\d{2})/(\d{2})/(\d{4})\|(\d+(?:\.\d+)?)\|(\d+(?:\.\d+)?)\|?\s*", body)
    if not m:
        raise ExchangeRateError(f"Respuesta de SUNAT no reconocida: {body[:80]!r}")
    sell = float(m[5])
    if sell <= 0:
        raise ExchangeRateError(f"Tipo de cambio no válido: {m[5]}")
    return {
        "currency": "USD",
        "date": f"{m[3]}-{m[2]}-{m[1]}",
        "buy": float(m[4]),
        "sell": sell,
        "source": "sunat",
    }


class SunatExchangeRateSource:
    def __init__(
        self,
        *,
        url: str = SUNAT_EXCHANGE_RATE_URL,
        timeout_s: float = 20.0,
        client: httpx.Client | None = None,
    ):
        self.url = url
        self.http = client or httpx.Client(timeout=timeout_s)

    def fetch_today(self) -> dict[str, Any]:
        try:
            res = self.http.get(
                self.url, headers={"user-agent": "InvoiceBot", "accept": "text/plain"}
            )
        except httpx.HTTPError as error:
            raise ExchangeRateError(
                f"No se pudo descargar el tipo de cambio de SUNAT: {error}"
            ) from error
        if res.status_code >= 400:
            raise ExchangeRateError(f"SUNAT respondió {res.status_code} al pedir el tipo de cambio")
        return parse_sunat_exchange_rate(res.text)


def parse_date(value: str) -> str | None:
    """DD/MM/AAAA o AAAA-MM-DD → AAAA-MM-DD; None si no es una fecha que exista."""
    iso: str | None = None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        iso = value
    elif m := re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", value):
        iso = f"{m[3]}-{int(m[2]):02d}-{int(m[1]):02d}"
    if not iso:
        return None
    try:
        date.fromisoformat(iso)
    except ValueError:
        return None
    return iso


def parse_exchange_rate_csv(
    text: str, *, currency: str = "USD"
) -> tuple[list[dict[str, Any]], list[str]]:
    """Una fila por día: fecha, compra, venta (una sola columna de importe = venta).

    Separador coma, punto y coma, barra o tabulador; encabezado opcional.
    """
    rates: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, line in enumerate(re.split(r"\r?\n", text)):
        cells = [c.strip() for c in re.split(r"[,;|\t]", line) if c.strip()]
        if not cells:
            continue
        day = parse_date(cells[0])
        if not day:
            if index > 0 or re.search(r"\d", cells[0]):
                errors.append(f"Línea {index + 1}: fecha no válida")
            continue
        try:
            numbers = [float(c) for c in cells[1:]]
        except ValueError:
            numbers = []
        if not numbers or any(n <= 0 for n in numbers):
            errors.append(f"Línea {index + 1}: importes no válidos")
            continue
        buy, sell = (None, numbers[0]) if len(numbers) == 1 else (numbers[0], numbers[1])
        rates.append({"currency": currency, "date": day, "buy": buy, "sell": sell, "source": "csv"})
    return rates, errors
