"""Tipo de cambio para registrar en soles los comprobantes en otra moneda.

Se usa el de venta publicado por SUNAT para la fecha de emisión o, si ese día
no hubo publicación (fines de semana y feriados), el último publicado antes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from invoicebot.domain.invoice import InvoiceData, NewIssue, Severity

#: Días hacia atrás que se buscan si la fecha de emisión no tiene publicación.
EXCHANGE_RATE_LOOKBACK_DAYS = 10
LOCAL_CURRENCY = "PEN"


@dataclass(frozen=True)
class ExchangeRate:
    rate: float
    date: str


def needs_exchange_rate(invoice: InvoiceData) -> bool:
    return invoice.currency is not None and invoice.currency != LOCAL_CURRENCY


def to_pen_cents(cents: int, rate: float) -> int:
    """Céntimos de la moneda original → céntimos de sol (tipo de cambio con 3 decimales)."""
    milli = math.floor(rate * 1000 + 0.5)
    product = cents * milli
    # Redondeo mitad hacia arriba (Math.round de JS), sin coma flotante.
    return (product + 500) // 1000 if product >= 0 else -((-product + 499) // 1000)


def check_exchange_rate(invoice: InvoiceData, rate: ExchangeRate | None) -> list[NewIssue]:
    if not needs_exchange_rate(invoice) or rate is not None:
        return []
    return [
        NewIssue(
            "FX_RATE_MISSING",
            Severity.WARNING,
            f"No hay tipo de cambio de {invoice.currency} para el "
            f"{invoice.issue_date or '(sin fecha)'}: los importes en soles no se pueden "
            "calcular. Cárgalo con: invoicebot fx set",
            None,
            invoice.currency,
        )
    ]


def is_exchange_dependent_rule(rule_code: str) -> bool:
    """Reglas que se recalculan al cargar tipos de cambio nuevos."""
    return rule_code.startswith(("FX_", "DETRACTION_"))
