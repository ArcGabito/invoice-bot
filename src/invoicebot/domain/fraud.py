"""Señales de fraude o error en cuentas por pagar (funciones puras).

No bloquean nada: generan advertencias ``FRAUD_*`` que una persona revisa. Cada señal
dice qué se comparó para que el revisor pueda descartarla rápido.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

FRAUD_PREFIX = "FRAUD_"


@dataclass(frozen=True)
class PastInvoice:
    invoice_id: int
    series: str | None
    number: str | None
    issue_date: date | None
    total_pen_cents: int


@dataclass(frozen=True)
class Signal:
    code: str
    message: str
    expected: str | None = None
    actual: str | None = None


def _money(cents: float) -> str:
    return f"S/ {cents / 100:,.2f}"


def detect(
    *,
    series: str | None,
    number: str | None,
    issue_date: date | None,
    total_pen_cents: int,
    history: Sequence[PastInvoice],
    new_supplier_threshold_cents: int = 1_000_000,
    min_history: int = 5,
    sigmas: float = 3.0,
) -> list[Signal]:
    """Señales para una factura frente al historial de su proveedor (sin ella misma)."""
    signals: list[Signal] = []
    if not history and total_pen_cents >= new_supplier_threshold_cents:
        signals.append(
            Signal(
                "FRAUD_NEW_SUPPLIER_HIGH_AMOUNT",
                "Primera factura de este proveedor y por un monto alto: confirma que sea un "
                "proveedor real y que el servicio se haya recibido",
                f"menos de {_money(new_supplier_threshold_cents)}",
                _money(total_pen_cents),
            )
        )
    amounts = [p.total_pen_cents for p in history if p.total_pen_cents > 0]
    if len(amounts) >= min_history:
        mean = statistics.fmean(amounts)
        deviation = statistics.pstdev(amounts)
        ceiling = mean + sigmas * max(deviation, mean * 0.1)
        if total_pen_cents > ceiling:
            signals.append(
                Signal(
                    "FRAUD_AMOUNT_OUT_OF_RANGE",
                    f"El importe está muy por encima de lo habitual para este proveedor "
                    f"({len(amounts)} facturas anteriores)",
                    f"hasta {_money(ceiling)}",
                    _money(total_pen_cents),
                )
            )
    twins = [
        p
        for p in history
        if p.total_pen_cents == total_pen_cents
        and p.issue_date == issue_date
        and (p.series, (p.number or "").lstrip("0")) != (series, (number or "").lstrip("0"))
    ]
    if twins:
        twin = twins[0]
        signals.append(
            Signal(
                "FRAUD_FUZZY_DUPLICATE",
                "Otra factura del mismo proveedor tiene el mismo importe y la misma fecha con "
                "otra serie o número: puede ser el mismo cobro dos veces",
                f"factura #{twin.invoice_id} {twin.series}-{twin.number}",
                f"{series}-{number}",
            )
        )
    return signals
