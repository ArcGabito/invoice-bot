"""Dependencias de los casos de uso (las crea invoicebot.context.AppContext)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from invoicebot.config import Config

if TYPE_CHECKING:
    from invoicebot.application.ports import AiProvider, DocumentExtractor, VisionExtractor
    from invoicebot.infrastructure.db.invoice_repository import InvoiceRepository


@dataclass
class Deps:
    config: Config
    repository: InvoiceRepository
    document_router: DocumentExtractor
    ai_provider: AiProvider
    vision_extractor: VisionExtractor | None
    #: Fecha local AAAA-MM-DD, para que la validación sea reproducible.
    today: str
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("invoicebot"))
    extra: dict[str, Any] = field(default_factory=dict)
