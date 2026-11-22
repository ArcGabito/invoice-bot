"""Verificación de comprobantes en SUNAT (estado del comprobante y del RUC emisor)."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from invoicebot.application.deps import Deps
from invoicebot.application.ports import SunatVerifier
from invoicebot.domain.sunat_status import evaluate_sunat_result
from invoicebot.errors import ConfigError, error_message
from invoicebot.infrastructure.sunat import SUPPORTED_DOCUMENT_TYPES


@dataclass
class SunatSummary:
    candidates: int = 0
    checked: int = 0
    accepted: int = 0
    voided: int = 0
    not_found: int = 0
    not_authorized: int = 0
    supplier_not_active: int = 0
    supplier_not_habido: int = 0
    failed: int = 0
    failures: list[dict[str, str]] = field(default_factory=list)
    duration_ms: int = 0

    def has_problems(self) -> bool:
        """Anulados, no informados, no autorizados o consultas fallidas."""
        return self.failed + self.voided + self.not_found + self.not_authorized > 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def verify_sunat(
    deps: Deps,
    *,
    client: SunatVerifier,
    month: str | None = None,
    force: bool = False,
    limit: int | None = None,
    delay_s: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
    on_progress: Callable[[int, int, str, dict[str, Any]], None] | None = None,
) -> SunatSummary:
    """Consultas secuenciales; un fallo puntual se registra y el lote sigue.

    Credenciales inválidas detienen el lote: todas las consultas siguientes fallarían igual.
    """
    started = time.monotonic()
    repo = deps.repository
    delay = deps.config.sunat.delay_s if delay_s is None else delay_s
    summary = SunatSummary()
    invoices = repo.list_invoices_for_sunat_check(
        document_types=SUPPORTED_DOCUMENT_TYPES, month=month, force=force, limit=limit
    )
    summary.candidates = len(invoices)
    for index, invoice in enumerate(invoices):
        if index > 0 and delay > 0:
            sleep(delay)
        label = f"{invoice['series']}-{invoice['number']}"
        outcome: dict[str, Any]
        try:
            result = client.validate_receipt(
                supplier_ruc=invoice["supplier_ruc"],
                document_type=invoice["document_type"],
                series=invoice["series"],
                number=invoice["number"],
                issue_date=invoice["issue_date"].isoformat(),
                total_cents=invoice["total_cents"],
            )
            receipt_status, issues = evaluate_sunat_result(
                result, document_type=invoice["document_type"]
            )
            status = repo.record_sunat_check(
                invoice_id=invoice["id"],
                result=result,
                receipt_status=receipt_status,
                issues=issues,
            )
            summary.checked += 1
            if receipt_status in ("ACEPTADO", "AUTORIZADO"):
                summary.accepted += 1
            elif receipt_status == "ANULADO":
                summary.voided += 1
            elif receipt_status == "NO EXISTE":
                summary.not_found += 1
            elif receipt_status == "NO AUTORIZADO":
                summary.not_authorized += 1
            codes = {i.rule_code for i in issues}
            summary.supplier_not_active += "SUNAT_SUPPLIER_NOT_ACTIVE" in codes
            summary.supplier_not_habido += "SUNAT_SUPPLIER_NOT_HABIDO" in codes
            outcome = {"receipt_status": receipt_status, "validation_status": status}
        except Exception as error:
            kind = getattr(error, "kind", "unknown")
            message = error_message(error)
            repo.record_sunat_check(invoice_id=invoice["id"], error=(kind, message))
            summary.failed += 1
            summary.failures.append({"invoice": label, "message": message})
            deps.logger.warning(
                "consulta a SUNAT fallida: %s", message, extra={"invoice_id": invoice["id"]}
            )
            if kind == "sunat_auth":
                raise ConfigError(message) from error
            outcome = {"error": message}
        if on_progress:
            on_progress(index + 1, len(invoices), label, outcome)
    summary.duration_ms = int((time.monotonic() - started) * 1000)
    return summary
