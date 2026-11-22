"""Trazas distribuidas con OpenTelemetry (opcional).

Se activa con las variables estándar de OpenTelemetry, sin configuración propia:

- ``OTEL_EXPORTER_OTLP_ENDPOINT`` (p. ej. ``http://otel-collector:4318``): destino OTLP/HTTP.
  Azure Monitor, Grafana Tempo, Jaeger o Honeycomb lo reciben con un colector.
- ``OTEL_SERVICE_NAME`` (por defecto ``invoicebot-<proceso>``), ``OTEL_RESOURCE_ATTRIBUTES``,
  ``OTEL_TRACES_SAMPLER`` y ``OTEL_EXPORTER_OTLP_HEADERS`` como en cualquier servicio.

Instrumenta FastAPI, las llamadas HTTP (httpx: IA, SUNAT, webhooks) y SQL Server
(SQLAlchemy). Cada trabajo de la cola es un span ``job <tarea>``; los logs JSON llevan
``trace_id`` y ``span_id`` para saltar del log a la traza.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

_ENABLED = False


def enabled() -> bool:
    return _ENABLED


def setup(process: str, *, engine: Any | None = None, app: Any | None = None) -> bool:
    """Configura el exportador una vez por proceso. False si OTEL no está configurado."""
    global _ENABLED
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") and not os.environ.get(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"
    ):
        return False
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    from invoicebot import __version__

    if not _ENABLED:
        resource = Resource.create(
            {
                "service.name": os.environ.get("OTEL_SERVICE_NAME", f"invoicebot-{process}"),
                "service.version": __version__,
                "service.namespace": "invoicebot",
            }
        )
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)
        HTTPXClientInstrumentor().instrument()
        _ENABLED = True
    if engine is not None:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        SQLAlchemyInstrumentor().instrument(engine=engine)
    if app is not None:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz,metrics")
    return True


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[None]:
    """Span con atributos (no hace nada si OTEL no está activo)."""
    if not _ENABLED:
        yield
        return
    from opentelemetry import trace

    tracer = trace.get_tracer("invoicebot")
    with tracer.start_as_current_span(name) as current:
        for key, value in attributes.items():
            if value is not None:
                current.set_attribute(key, value)
        yield


class TraceContextFilter(logging.Filter):
    """Agrega trace_id y span_id a cada registro si hay una traza activa."""

    def filter(self, record: logging.LogRecord) -> bool:
        if _ENABLED:
            from opentelemetry import trace

            context = trace.get_current_span().get_span_context()
            if context.is_valid:
                record.trace_id = format(context.trace_id, "032x")
                record.span_id = format(context.span_id, "016x")
        return True
