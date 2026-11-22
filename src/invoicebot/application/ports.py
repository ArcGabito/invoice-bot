"""Puertos: lo que los casos de uso esperan de los adaptadores."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass
class AiUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass
class AiExtractionResult:
    #: Datos que cumplen el esquema de extracción (dict con la forma de Extraction).
    data: dict[str, Any]
    #: Respuesta textual del modelo, para auditoría.
    raw: str
    #: Modelo que realmente respondió (puede ser el de respaldo).
    model: str
    usage: AiUsage = field(default_factory=AiUsage)


class AiProvider(Protocol):
    name: str
    model: str

    def extract(self, *, text: str, file_name: str, ocr: bool = False) -> AiExtractionResult:
        """Lanza ExtractionError cuando falla."""
        ...

    def ping(self) -> str: ...


class VisionExtractor(AiProvider, Protocol):
    def extract_from_file(
        self, *, file_path: str, mime_type: str, file_name: str
    ) -> AiExtractionResult: ...


@dataclass
class OcrResult:
    text: str
    confidence: int | None
    page_count: int | None
    pages_read: int


class OcrEngine(Protocol):
    name: str

    def recognize(self, *, file_path: str, mime_type: str) -> OcrResult: ...

    def ping(self) -> str: ...


@dataclass
class OcrInfo:
    engine: str
    confidence: int | None
    pages_read: int


@dataclass
class TextExtraction:
    #: True si no hay texto suficiente sin OCR.
    needs_ocr: bool
    text: str | None
    #: pdf_text | ocr | xml
    method: str | None
    page_count: int | None
    #: Caracteres visibles (bytes en XML).
    chars: int
    ocr_attempted: bool = False
    ocr: OcrInfo | None = None
    #: Solo en XML: datos con la forma de Extraction; no hace falta IA.
    structured: Any = None


class DocumentExtractor(Protocol):
    @property
    def can_ocr(self) -> bool: ...

    def extract_text(self, *, file_path: str, mime_type: str) -> TextExtraction:
        """Lanza DocumentReadError si el archivo está dañado o cifrado."""
        ...


@dataclass(frozen=True)
class MailMessageRef:
    id: str
    received_at: datetime | None


class MailSource(Protocol):
    provider: str

    def connect(self) -> str:
        """Inicia sesión y devuelve la dirección del buzón."""
        ...

    def list_messages(self, *, since: datetime) -> list[MailMessageRef]: ...

    def fetch_raw(self, message_id: str) -> bytes: ...

    def move(self, message_id: str, folder: str) -> None: ...

    def close(self) -> None: ...


class SunatVerifier(Protocol):
    def validate_receipt(
        self,
        *,
        supplier_ruc: str,
        document_type: str,
        series: str,
        number: str,
        issue_date: str,
        total_cents: int | None,
    ) -> Any: ...


class ExchangeRateSource(Protocol):
    def fetch_today(self) -> dict[str, Any]: ...
