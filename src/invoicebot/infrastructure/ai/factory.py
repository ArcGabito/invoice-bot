"""Crea el proveedor de IA configurado (INVOICEBOT_AI_PROVIDER)."""

from __future__ import annotations

from invoicebot.application.ports import AiProvider
from invoicebot.config import Config


def create_ai_provider(config: Config) -> AiProvider:
    if config.ai_provider == "mock":
        from invoicebot.infrastructure.ai.mock_provider import MockProvider

        return MockProvider()
    if config.ai_provider == "ollama":
        from invoicebot.infrastructure.ai.ollama_provider import OllamaProvider

        o = config.ollama
        return OllamaProvider(
            url=o.url, model=o.model, think=o.think, num_ctx=o.num_ctx, timeout_s=o.timeout_s
        )
    from invoicebot.infrastructure.ai.anthropic_provider import AnthropicProvider

    return AnthropicProvider(model=config.anthropic.model, effort=config.anthropic.effort)


def create_vision_extractor(config: Config) -> AiProvider | None:
    """Lectura con visión de Claude (solo con INVOICEBOT_OCR_ENGINE=claude)."""
    if config.ocr.engine != "claude":
        return None
    from invoicebot.infrastructure.ai.anthropic_provider import AnthropicProvider

    return AnthropicProvider(model=config.anthropic.model, effort=config.anthropic.effort)
