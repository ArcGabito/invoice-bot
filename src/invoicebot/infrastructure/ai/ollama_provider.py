"""Proveedor local mediante la API HTTP de Ollama: los documentos no salen del equipo.

Decisiones tomadas tras probar con modelos locales reales:
- `think: false` por defecto: los modelos con razonamiento tardan minutos y pueden
  agotar el contexto antes de escribir el JSON.
- `num_ctx` explícito: el valor por defecto de Ollama (4096) se queda corto.
- `format` con el JSON Schema de Extraction y validación con el mismo modelo Pydantic.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import ValidationError

from invoicebot.application.ports import AiExtractionResult, AiUsage
from invoicebot.errors import ExtractionError
from invoicebot.infrastructure.ai.prompt import SYSTEM_PROMPT, build_user_prompt
from invoicebot.infrastructure.ai.schema import Extraction


@dataclass
class ChatReply:
    content: str
    thinking: str
    done_reason: str | None
    model: str | None
    usage: AiUsage


def parse_chat_response(body: str) -> ChatReply:
    """Une los fragmentos NDJSON de /api/chat (o una respuesta única)."""
    content, thinking = [], []
    last: dict[str, Any] | None = None
    for line in body.splitlines():
        if not line.strip():
            continue
        chunk = json.loads(line)
        if chunk.get("error"):
            raise RuntimeError(chunk["error"])
        message = chunk.get("message") or {}
        content.append(message.get("content") or "")
        thinking.append(message.get("thinking") or "")
        last = chunk
    if not last or not last.get("done"):
        raise RuntimeError("la respuesta terminó antes de completarse")
    return ChatReply(
        "".join(content),
        "".join(thinking),
        last.get("done_reason"),
        last.get("model"),
        AiUsage(last.get("prompt_eval_count"), last.get("eval_count")),
    )


class OllamaProvider:
    name = "ollama"

    def __init__(
        self,
        *,
        url: str,
        model: str,
        think: bool = False,
        num_ctx: int = 8192,
        timeout_s: float = 600.0,
        client: httpx.Client | None = None,
    ):
        self.url = url.rstrip("/")
        self.model = model
        self.think = think
        self.num_ctx = num_ctx
        self.client = client or httpx.Client(timeout=httpx.Timeout(timeout_s, connect=10.0))
        self.json_schema = Extraction.model_json_schema()

    def _post(self, body: dict[str, Any]) -> httpx.Response:
        try:
            return self.client.post(f"{self.url}/api/chat", json=body)
        except httpx.HTTPError as error:
            raise ExtractionError(
                f"No se pudo conectar con Ollama en {self.url}: {error}",
                kind="transient",
                retryable=True,
            ) from error

    def extract(self, *, text: str, file_name: str, ocr: bool = False) -> AiExtractionResult:
        body: dict[str, Any] = {
            "model": self.model,
            "stream": False,
            "think": self.think,
            "format": self.json_schema,
            "options": {"temperature": 0, "num_ctx": self.num_ctx},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": build_user_prompt(text=text, file_name=file_name, ocr=ocr),
                },
            ],
        }
        res = self._post(body)
        if res.status_code == 400 and re.search("think", res.text, re.IGNORECASE):
            # Modelos sin soporte de razonamiento rechazan el parámetro `think`.
            body.pop("think")
            res = self._post(body)
        if res.status_code >= 400:
            retryable = res.status_code >= 500 or res.status_code == 429
            raise ExtractionError(
                f"Ollama respondió {res.status_code}: {res.text[:200]}",
                kind="transient" if retryable else "provider_request",
                retryable=retryable,
            )
        try:
            reply = parse_chat_response(res.text)
        except (RuntimeError, json.JSONDecodeError) as error:
            raise ExtractionError(
                f"Respuesta de Ollama interrumpida: {error}", kind="transient", retryable=True
            ) from error
        if reply.done_reason == "length":
            raise ExtractionError(
                f"La respuesta quedó truncada: aumenta INVOICEBOT_OLLAMA_NUM_CTX (ahora {self.num_ctx})",
                kind="invalid_output",
                raw_response=reply.content,
            )
        if not reply.content.strip() and reply.thinking:
            raise ExtractionError(
                "El modelo usó la respuesta solo para razonar; usa INVOICEBOT_OLLAMA_THINK=false",
                kind="invalid_output",
                raw_response=reply.thinking[:2000],
            )
        try:
            data = Extraction.model_validate_json(reply.content).model_dump()
        except ValidationError as error:
            first = error.errors()[0]["msg"] if error.errors() else str(error)
            kind_message = (
                "Ollama devolvió JSON inválido"
                if error.errors() and error.errors()[0]["type"] == "json_invalid"
                else f"La respuesta no cumple el esquema: {first}"
            )
            raise ExtractionError(
                kind_message, kind="invalid_output", retryable=True, raw_response=reply.content
            ) from error
        return AiExtractionResult(data, reply.content, reply.model or self.model, reply.usage)

    def ping(self) -> str:
        res = self.client.get(f"{self.url}/api/tags", timeout=5.0)
        if res.status_code >= 400:
            raise RuntimeError(f"Ollama respondió {res.status_code}")
        names = [m["name"] for m in res.json().get("models", [])]
        if not any(n in (self.model, f"{self.model}:latest") for n in names):
            available = f" (disponibles: {', '.join(names)})" if names else ""
            raise RuntimeError(
                f"El modelo {self.model} no está descargado. Ejecuta: ollama pull {self.model}{available}"
            )
        thinking = "activado" if self.think else "desactivado"
        return f"modelo {self.model} disponible (razonamiento {thinking}, contexto {self.num_ctx})"
