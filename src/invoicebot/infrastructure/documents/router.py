"""Decide cómo obtener los datos de un documento.

- XML: se lee el comprobante electrónico UBL directamente, sin IA.
- PDF: se extrae el texto nativo; si no alcanza el mínimo, se pasa por OCR.
- Imagen: se pasa por OCR.
Sin motor de OCR, lo que no tiene texto queda como `needs_ocr`.
"""

from __future__ import annotations

import re
from pathlib import Path

from invoicebot.application.ports import OcrEngine, OcrInfo, TextExtraction
from invoicebot.infrastructure.documents.pdf import extract_pdf_text
from invoicebot.infrastructure.documents.scanner import XML_MIME_TYPE
from invoicebot.infrastructure.documents.ubl import parse_ubl


def visible_chars(text: str) -> int:
    return len(re.sub(r"\s", "", text))


class DocumentRouter:
    def __init__(self, *, min_text_chars: int, ocr_engine: OcrEngine | None = None):
        self.min_text_chars = min_text_chars
        self.ocr_engine = ocr_engine

    @property
    def can_ocr(self) -> bool:
        return self.ocr_engine is not None

    def extract_text(self, *, file_path: str, mime_type: str) -> TextExtraction:
        if mime_type == XML_MIME_TYPE:
            data = Path(file_path).read_bytes()
            return TextExtraction(
                needs_ocr=False,
                text=None,
                method="xml",
                page_count=None,
                chars=len(data),
                structured=parse_ubl(data),
            )

        page_count: int | None = None
        if mime_type == "application/pdf":
            native = extract_pdf_text(file_path)
            page_count = native.page_count
            chars = visible_chars(native.text)
            if chars >= self.min_text_chars:
                return TextExtraction(False, native.text, "pdf_text", page_count, chars)

        # Imagen o PDF escaneado.
        if self.ocr_engine is None:
            return TextExtraction(True, None, None, page_count, 0)
        result = self.ocr_engine.recognize(file_path=file_path, mime_type=mime_type)
        chars = visible_chars(result.text)
        enough = chars >= self.min_text_chars
        return TextExtraction(
            needs_ocr=not enough,
            text=result.text if enough else None,
            method="ocr" if enough else None,
            page_count=result.page_count,
            chars=chars,
            ocr_attempted=True,
            ocr=OcrInfo(self.ocr_engine.name, result.confidence, result.pages_read),
        )
