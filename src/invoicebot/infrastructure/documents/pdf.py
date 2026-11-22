"""Texto nativo y dibujo de páginas de PDF con PDFium (pypdfium2)."""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Any

import pypdfium2 as pdfium

from invoicebot.errors import DocumentReadError


@dataclass(frozen=True)
class PdfText:
    text: str
    page_count: int


#: Glifos sin equivalente Unicode (U+0000, U+FFFE, U+FFFD). Algunos generadores
#: (p. ej. las facturas hechas con Stripe, como las de Cloudflare) no declaran el
#: texto de guiones ni paréntesis: "IN-45946823" llegaba como "IN\x0045946823".
_UNMAPPED = "\x00\ufffe\ufffd"
#: PDFium no es seguro entre hilos: cada uso va bajo este candado (con varios hilos
#: a la vez termina en un fallo de segmentación).
_PDFIUM_LOCK = threading.RLock()

_BETWEEN_ALNUM = re.compile(rf"(?<=[^\W_])[{_UNMAPPED}]+(?=[^\W_])")


def resolve_unmapped_glyphs(line: str) -> str:
    """Entre dos letras o dígitos casi siempre es un guion; en otra posición se descarta."""
    line = _BETWEEN_ALNUM.sub("-", line)
    line = re.sub(rf"[{_UNMAPPED}]", " ", line)
    return re.sub(r" {2,}", " ", line)


def _open(path: str) -> Any:
    try:
        return pdfium.PdfDocument(path)
    except pdfium.PdfiumError as error:
        message = str(error).lower()
        reason = (
            "El PDF está protegido con contraseña"
            if "password" in message
            else "El PDF está dañado o no se puede leer"
        )
        raise DocumentReadError(f"{reason}: {error}") from error


def extract_pdf_text(path: str) -> PdfText:
    """Texto de todas las páginas, línea a línea, separando páginas con una línea en blanco."""
    with _PDFIUM_LOCK:
        return _extract_pdf_text(path)


def _extract_pdf_text(path: str) -> PdfText:
    pdf = _open(path)
    try:
        pages: list[str] = []
        for index in range(len(pdf)):
            page = pdf[index]
            textpage = page.get_textpage()
            try:
                raw = textpage.get_text_bounded() if hasattr(textpage, "get_text_bounded") else ""
                if not raw:
                    raw = textpage.get_text_range()
            finally:
                textpage.close()
                page.close()
            lines = [resolve_unmapped_glyphs(line).strip() for line in re.split(r"\r\n|\r|\n", raw)]
            pages.append("\n".join(line for line in lines if line))
        return PdfText("\n\n".join(pages).strip(), len(pdf))
    finally:
        pdf.close()


def render_pdf_pages(path: str, *, max_pages: int = 3, scale: float = 3.0) -> tuple[list[Any], int]:
    """Imágenes PIL de las primeras `max_pages` páginas (escala 3 ≈ 216 ppp) y total de páginas."""
    with _PDFIUM_LOCK:
        return _render_pdf_pages(path, max_pages=max_pages, scale=scale)


def _render_pdf_pages(path: str, *, max_pages: int, scale: float) -> tuple[list[Any], int]:
    pdf = _open(path)
    try:
        images = []
        for index in range(min(len(pdf), max_pages)):
            page = pdf[index]
            try:
                bitmap = page.render(scale=scale, grayscale=True)
                images.append(bitmap.to_pil())
            finally:
                page.close()
        return images, len(pdf)
    finally:
        pdf.close()
