"""Tipos de cambio: búsqueda, carga y aplicación a las facturas que los esperaban."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from invoicebot.domain.exchange_rate import (
    ExchangeRate,
    is_exchange_dependent_rule,
    needs_exchange_rate,
)
from invoicebot.domain.invoice import InvoiceData
from invoicebot.domain.validation import exchange_dependent_issues

if TYPE_CHECKING:
    from invoicebot.application.deps import Deps
    from invoicebot.infrastructure.db.invoice_repository import InvoiceRepository


def lookup_exchange_rate(
    repository: InvoiceRepository, invoice: InvoiceData
) -> ExchangeRate | None:
    """Tipo de cambio de la fecha de emisión; None si está en soles o no lo hay."""
    if not needs_exchange_rate(invoice) or not invoice.issue_date or not invoice.currency:
        return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", invoice.issue_date):
        return None
    return repository.find_exchange_rate(invoice.currency, invoice.issue_date)


def apply_exchange_rates(deps: Deps) -> dict[str, int]:
    """Aplica los tipos de cambio disponibles y recalcula las incidencias FX_* y DETRACTION_*."""
    repo = deps.repository
    updated = missing = 0
    for row in repo.list_invoices_awaiting_exchange_rate():
        invoice = InvoiceData.from_mapping(row)
        rate = lookup_exchange_rate(repo, invoice)
        if rate is None:
            missing += 1
            continue
        with repo.transaction() as tx:
            tx.set_invoice_exchange_rate(row["id"], rate)
            tx.replace_rule_issues(
                row["id"],
                exchange_dependent_issues(invoice, rate),
                lambda code: not is_exchange_dependent_rule(code),
            )
            tx.recompute_validation_status(row["id"])
        updated += 1
    if updated:
        deps.logger.info(
            "tipo de cambio aplicado a %s facturas (%s sin tipo de cambio)", updated, missing
        )
    return {"updated": updated, "missing": missing}


def save_exchange_rates(deps: Deps, rates: Iterable[dict[str, Any]]) -> dict[str, int]:
    """Guarda tipos de cambio y los aplica a las facturas que los esperaban."""
    saved = deps.repository.upsert_exchange_rates(list(rates))
    return {"saved": saved, **apply_exchange_rates(deps)}
