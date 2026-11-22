"""Proveedor Claude (Anthropic) con salida estructurada validada por Pydantic.

Usa el respaldo del lado del servidor (`fallbacks="default"`): si el modelo
rechaza una petición por sus salvaguardas, la API la reintenta con el modelo
recomendado para esa categoría dentro de la misma llamada. El modelo que
realmente respondió se guarda en extraction_runs.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import anthropic

from invoicebot.application.ports import AiExtractionResult, AiUsage
from invoicebot.errors import ExtractionError
from invoicebot.infrastructure.ai.prompt import (
    SYSTEM_PROMPT,
    build_user_prompt,
    build_vision_prompt,
)
from invoicebot.infrastructure.ai.schema import Extraction

#: Límites de la API para adjuntos (las imágenes, más estrictos que los PDF).
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_PDF_BYTES = 30 * 1024 * 1024
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, *, model: str, effort: str, client: Any | None = None):
        self.model = model
        self.effort = effort
        self.client: Any = client if client is not None else anthropic.Anthropic(max_retries=3)

    def extract(self, *, text: str, file_name: str, ocr: bool = False) -> AiExtractionResult:
        return self._request(build_user_prompt(text=text, file_name=file_name, ocr=ocr))

    def extract_from_file(
        self, *, file_path: str, mime_type: str, file_name: str
    ) -> AiExtractionResult:
        """Lectura con visión: la imagen o el PDF se envían tal cual, sin OCR previo."""
        data = Path(file_path).read_bytes()
        limit = MAX_PDF_BYTES if mime_type == "application/pdf" else MAX_IMAGE_BYTES
        if len(data) > limit:
            raise ExtractionError(
                f"El archivo pesa {len(data) / 1048576:.1f} MB; el máximo para la lectura con "
                f"visión es {limit // 1048576} MB",
                kind="file_too_large",
            )
        encoded = base64.standard_b64encode(data).decode()
        attachment: dict[str, Any]
        if mime_type == "application/pdf":
            attachment = {
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": encoded},
            }
        else:
            attachment = {
                "type": "image",
                "source": {"type": "base64", "media_type": mime_type, "data": encoded},
            }
        return self._request(
            [attachment, {"type": "text", "text": build_vision_prompt(file_name=file_name)}]
        )

    def _request(self, content: str | list[dict[str, Any]]) -> AiExtractionResult:
        try:
            response: Any = self.client.beta.messages.parse(
                model=self.model,
                max_tokens=16000,
                betas=[FALLBACK_BETA],
                fallbacks="default",
                system=SYSTEM_PROMPT,
                output_config={"effort": self.effort},
                output_format=Extraction,
                messages=[{"role": "user", "content": content}],
            )
        except anthropic.AnthropicError as error:
            raise to_extraction_error(error) from error

        raw = "".join(b.text for b in response.content if getattr(b, "type", None) == "text")
        usage = AiUsage(
            input_tokens=getattr(response.usage, "input_tokens", None),
            output_tokens=getattr(response.usage, "output_tokens", None),
        )
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) or "sin categoría"
            raise ExtractionError(
                f"El modelo rechazó el documento ({category})", kind="refused", raw_response=raw
            )
        if response.stop_reason == "max_tokens":
            raise ExtractionError(
                "La respuesta del modelo quedó truncada",
                kind="invalid_output",
                retryable=True,
                raw_response=raw,
            )
        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            raise ExtractionError(
                "La respuesta no cumple el esquema de extracción",
                kind="invalid_output",
                retryable=True,
                raw_response=raw,
            )
        data = (
            parsed.model_dump()
            if isinstance(parsed, Extraction)
            else Extraction.model_validate(parsed).model_dump()
        )
        return AiExtractionResult(data=data, raw=raw, model=response.model, usage=usage)

    def ping(self) -> str:
        """Comprueba credenciales y modelo sin consumir tokens."""
        try:
            info = self.client.models.retrieve(self.model)
        except anthropic.APIError as error:
            raise to_extraction_error(error) from error
        except Exception as error:
            raise RuntimeError(
                f"{error}\n  Define ANTHROPIC_API_KEY en .env, ejecuta `ant auth login` o usa "
                "--provider ollama"
            ) from error
        return f"modelo {info.id} disponible"


def to_extraction_error(error: BaseException) -> ExtractionError:
    """Traduce los errores del SDK: qué se reintenta y qué no."""
    if isinstance(error, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
        return ExtractionError(
            f"Credenciales de Anthropic inválidas: {error}", kind="provider_auth"
        )
    if isinstance(error, anthropic.NotFoundError):
        return ExtractionError(f"Modelo no encontrado: {error}", kind="provider_config")
    if isinstance(error, anthropic.BadRequestError):
        return ExtractionError(f"Petición rechazada por la API: {error}", kind="provider_request")
    if isinstance(error, anthropic.RateLimitError | anthropic.InternalServerError):
        return ExtractionError(
            f"Error temporal de la API ({error.status_code}): {error}",
            kind="transient",
            retryable=True,
        )
    if isinstance(error, anthropic.APIConnectionError):
        return ExtractionError(
            f"No se pudo conectar con Anthropic: {error}", kind="transient", retryable=True
        )
    if isinstance(error, anthropic.APIStatusError):
        return ExtractionError(
            f"Error de la API ({error.status_code}): {error}", kind="transient", retryable=True
        )
    # Errores del SDK al interpretar la salida (JSON inválido, esquema no cumplido).
    return ExtractionError(
        f"Salida del modelo no interpretable: {error}", kind="invalid_output", retryable=True
    )
