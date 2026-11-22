"""OCR local con Tesseract (pytesseract): nada sale del equipo.

Requiere el ejecutable de Tesseract con los idiomas configurados:
- Docker / Debian: `apt-get install tesseract-ocr tesseract-ocr-spa` (incluido en la imagen).
- Windows: instalador de UB Mannheim; INVOICEBOT_OCR_TESSERACT_CMD si no está en el PATH.
"""

from __future__ import annotations

import re
import shutil
import threading
from typing import Any

from invoicebot.application.ports import OcrEngine, OcrResult
from invoicebot.config import Config
from invoicebot.errors import ConfigError
from invoicebot.infrastructure.documents.pdf import render_pdf_pages


class TesseractOcr:
    name = "tesseract"

    def __init__(
        self,
        *,
        languages: tuple[str, ...] = ("spa", "eng"),
        max_pages: int = 3,
        scale: float = 3.0,
        command: str | None = None,
    ):
        import pytesseract

        self._tesseract: Any = pytesseract
        if command:
            pytesseract.pytesseract.tesseract_cmd = command
        self.languages = languages
        self.max_pages = max_pages
        self.scale = scale
        self._checked = False
        self._lock = threading.Lock()

    def _check(self) -> None:
        with self._lock:
            if self._checked:
                return
            try:
                installed = set(self._tesseract.get_languages(config=""))
            except Exception as error:
                raise ConfigError(
                    "No se encontró Tesseract. Instálalo (Windows: UB Mannheim; Linux: "
                    "tesseract-ocr) o define INVOICEBOT_OCR_TESSERACT_CMD, o usa "
                    f"INVOICEBOT_OCR_ENGINE=none. Detalle: {error}"
                ) from error
            missing = [lang for lang in self.languages if lang not in installed]
            if missing:
                raise ConfigError(
                    f"Faltan los datos de Tesseract de: {', '.join(missing)} "
                    "(Debian: apt-get install tesseract-ocr-spa)"
                )
            self._checked = True

    def _deskew(self, image: Any) -> Any:
        """Endereza fotos y escaneos girados 90/180/270° (detección de orientación)."""
        try:
            osd = self._tesseract.image_to_osd(image, config="--psm 0")
        except Exception:
            return image  # Sin datos OSD o poco texto: se lee tal cual.
        m = re.search(r"Rotate:\s*(\d+)", osd)
        angle = int(m[1]) if m else 0
        return image.rotate(-angle, expand=True) if angle else image

    def recognize(self, *, file_path: str, mime_type: str) -> OcrResult:
        from PIL import Image

        self._check()
        if mime_type == "application/pdf":
            images, page_count = render_pdf_pages(
                file_path, max_pages=self.max_pages, scale=self.scale
            )
        else:
            with Image.open(file_path) as img:
                images, page_count = [img.convert("L")], 1
        lang = "+".join(self.languages)
        pages: list[tuple[str, float]] = []
        for original in images:
            image = self._deskew(original)
            data = self._tesseract.image_to_data(
                image, lang=lang, output_type=self._tesseract.Output.DICT
            )
            text = self._tesseract.image_to_string(image, lang=lang).strip()
            words = [
                (str(w), float(c))
                for w, c in zip(data["text"], data["conf"], strict=False)
                if str(w).strip() and float(c) >= 0
            ]
            weight = sum(len(w) for w, _ in words)
            confidence = sum(len(w) * c for w, c in words) / weight if weight else 0.0
            pages.append((text, confidence))
        # Media ponderada por la cantidad de texto de cada página.
        weights = [max(len(re.sub(r"\s", "", t)), 1) for t, _ in pages]
        total = sum(weights) or 1
        confidence = sum(c * w for (_, c), w in zip(pages, weights, strict=False)) / total
        return OcrResult(
            text="\n\n".join(t for t, _ in pages).strip(),
            confidence=round(confidence),
            page_count=page_count,
            pages_read=len(pages),
        )

    def ping(self) -> str:
        self._check()
        version = self._tesseract.get_tesseract_version()
        return f"Tesseract {version} listo (idiomas: {', '.join(self.languages)}; sin internet)"


def tesseract_available(command: str | None = None) -> bool:
    return bool(command or shutil.which("tesseract"))


def create_ocr_engine(config: Config) -> OcrEngine | None:
    """Motor local de OCR (la visión de Claude no es un motor de OCR: ver factory de IA)."""
    if config.ocr.engine != "tesseract":
        return None
    return TesseractOcr(
        languages=config.ocr.languages,
        max_pages=config.ocr.max_pages,
        scale=config.ocr.scale,
        command=config.ocr.tesseract_cmd,
    )
