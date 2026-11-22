"""Revisión humana: aprobar, rechazar, reabrir, corregir y resolver incidencias.

Cada acción se aplica en una transacción junto con su registro en
review_actions: no hay cambios sin auditoría ni auditoría sin cambios.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from invoicebot.application.deps import Deps
from invoicebot.application.exchange_rates import lookup_exchange_rate
from invoicebot.domain.duplicates import business_key
from invoicebot.domain.invoice import DocumentStatus, InvoiceData, NewIssue, Severity
from invoicebot.domain.review import (
    SUNAT_RELEVANT_FIELDS,
    CorrectionError,
    ReviewStatus,
    is_preserved_on_revalidation,
    parse_correction,
    split_assignment,
)
from invoicebot.domain.validation import validate_invoice
from invoicebot.errors import ReviewError
from invoicebot.infrastructure.db.invoice_repository import InvoiceRepository
from invoicebot.money import format_cents


def _require_invoice(repo: InvoiceRepository, invoice_id: int | str) -> dict[str, Any]:
    try:
        number = int(invoice_id)
    except (TypeError, ValueError):
        number = -1
    invoice = repo.get_invoice_detail(number) if number > 0 else None
    if not invoice:
        raise ReviewError(f"No existe la factura #{invoice_id}")
    return invoice


def _require_user(user: str | None) -> str:
    if not user or not str(user).strip():
        raise ReviewError("Falta identificar quién revisa")
    return str(user).strip()


def _open_issues(repo: InvoiceRepository, invoice_id: int) -> list[dict[str, Any]]:
    return [i for i in repo.get_issues(invoice_id) if not i["resolved_at"]]


def _note(note: str | None) -> str | None:
    return (note or "").strip() or None


def get_invoice_detail(deps: Deps, invoice_id: int | str) -> dict[str, Any]:
    """Datos, incidencias, historial, extracciones y origen en el correo."""
    repo = deps.repository
    invoice = _require_invoice(repo, invoice_id)
    document = repo.get_document(invoice["document_id"])
    return {
        "invoice": invoice,
        "issues": repo.get_issues(invoice["id"]),
        "actions": repo.list_review_actions(invoice["id"]),
        "mail_origins": repo.find_mail_origins(document["sha256"]) if document else [],
        "extraction_runs": [
            {
                k: r[k]
                for k in (
                    "id",
                    "provider",
                    "model",
                    "prompt_version",
                    "status",
                    "error_kind",
                    "duration_ms",
                    "created_at",
                )
            }
            for r in repo.list_extraction_runs(invoice["document_id"])
        ],
    }


def approve_invoice(
    deps: Deps,
    invoice_id: int | str,
    *,
    user: str | None,
    note: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Confirma la factura y resuelve sus advertencias; con errores exige `force` y una nota."""
    repo = deps.repository
    reviewer = _require_user(user)
    invoice = _require_invoice(repo, invoice_id)
    if invoice["review_status"] == ReviewStatus.REJECTED:
        raise ReviewError(
            f"La factura #{invoice['id']} está rechazada: reábrela antes de aprobarla"
        )
    errors = [i for i in _open_issues(repo, invoice["id"]) if i["severity"] == Severity.ERROR]
    if errors and not force:
        codes = ", ".join(e["rule_code"] for e in errors)
        raise ReviewError(
            f"La factura #{invoice['id']} tiene {len(errors)} error(es) sin resolver ({codes}): "
            "corrige los datos o aprueba con --force y una nota"
        )
    if errors and not _note(note):
        raise ReviewError(
            "Para aprobar una factura con errores hace falta una nota que lo justifique"
        )
    with repo.transaction() as tx:
        resolved = tx.resolve_open_issues(
            invoice["id"],
            severities=["error", "warning", "info"] if errors else ["warning", "info"],
            user=reviewer,
            note=_note(note) or "Aprobada en revisión",
        )
        tx.set_review_status(invoice["id"], ReviewStatus.APPROVED, reviewer)
        tx.record_review_action(
            invoice_id=invoice["id"], action="approve", user=reviewer, note=_note(note)
        )
        status = tx.recompute_validation_status(invoice["id"])
    return {
        "invoice_id": invoice["id"],
        "review_status": ReviewStatus.APPROVED,
        "validation_status": status,
        "resolved": resolved,
    }


def reject_invoice(
    deps: Deps, invoice_id: int | str, *, user: str | None, reason: str | None
) -> dict[str, Any]:
    """Deja de exportarse y de aparecer en la bandeja. Exige un motivo."""
    repo = deps.repository
    reviewer = _require_user(user)
    invoice = _require_invoice(repo, invoice_id)
    why = _note(reason)
    if not why:
        raise ReviewError("Indica el motivo del rechazo")
    with repo.transaction() as tx:
        tx.set_review_status(invoice["id"], ReviewStatus.REJECTED, reviewer)
        tx.record_review_action(invoice_id=invoice["id"], action="reject", user=reviewer, note=why)
    return {"invoice_id": invoice["id"], "review_status": ReviewStatus.REJECTED}


def reopen_invoice(
    deps: Deps, invoice_id: int | str, *, user: str | None, note: str | None = None
) -> dict[str, Any]:
    """Devuelve una factura aprobada o rechazada a pendiente y reabre sus incidencias."""
    repo = deps.repository
    reviewer = _require_user(user)
    invoice = _require_invoice(repo, invoice_id)
    if invoice["review_status"] == ReviewStatus.PENDING:
        raise ReviewError(f"La factura #{invoice['id']} ya está pendiente de revisión")
    with repo.transaction() as tx:
        reopened = tx.reopen_issues(invoice["id"])
        tx.set_review_status(invoice["id"], ReviewStatus.PENDING, reviewer)
        tx.record_review_action(
            invoice_id=invoice["id"], action="reopen", user=reviewer, note=_note(note)
        )
        status = tx.recompute_validation_status(invoice["id"])
    return {
        "invoice_id": invoice["id"],
        "review_status": ReviewStatus.PENDING,
        "validation_status": status,
        "reopened": reopened,
    }


def resolve_issue(
    deps: Deps, issue_id: int | str, *, user: str | None, note: str | None = None
) -> dict[str, Any]:
    repo = deps.repository
    reviewer = _require_user(user)
    try:
        number = int(issue_id)
    except (TypeError, ValueError):
        number = -1
    issue = repo.get_issue(number) if number > 0 else None
    if not issue:
        raise ReviewError(f"No existe la incidencia #{issue_id}")
    if issue["resolved_at"]:
        raise ReviewError(f"La incidencia #{issue_id} ya está resuelta")
    if issue["severity"] == Severity.ERROR and not _note(note):
        raise ReviewError("Para dar por resuelto un error hace falta una nota que lo justifique")
    with repo.transaction() as tx:
        tx.resolve_issue(issue["id"], user=reviewer, note=_note(note))
        tx.record_review_action(
            invoice_id=issue["invoice_id"],
            action="resolve_issue",
            user=reviewer,
            issue_id=issue["id"],
            field=issue["rule_code"],
            note=_note(note),
        )
        status = tx.recompute_validation_status(issue["invoice_id"])
    return {"invoice_id": issue["invoice_id"], "issue_id": issue["id"], "validation_status": status}


def _display(column: str, value: object) -> str:
    if value is None:
        return ""
    if column.endswith("_cents"):
        return format_cents(int(value))  # type: ignore[call-overload]
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def correct_invoice(
    deps: Deps,
    invoice_id: int | str,
    *,
    changes: Sequence[str | tuple[str, object]],
    user: str | None,
    note: str | None = None,
) -> dict[str, Any]:
    """Corrige datos y revalida con las mismas reglas que la extracción.

    `changes` acepta pares (campo, valor) o textos "campo=valor". Cada cambio queda
    en el historial con su valor anterior y el nuevo.
    """
    repo = deps.repository
    reviewer = _require_user(user)
    invoice = _require_invoice(repo, invoice_id)
    if invoice["review_status"] == ReviewStatus.REJECTED:
        raise ReviewError(
            f"La factura #{invoice['id']} está rechazada: reábrela antes de corregirla"
        )
    if not changes:
        raise ReviewError("Indica al menos un cambio (campo=valor)")
    try:
        parsed = [
            parse_correction(*(split_assignment(c) if isinstance(c, str) else c)) for c in changes
        ]
    except CorrectionError as error:
        raise ReviewError(str(error)) from error

    current = InvoiceData.from_mapping(invoice)
    effective = [p for p in parsed if current.get(p.column) != p.value]
    if not effective:
        return {
            "invoice_id": invoice["id"],
            "changed": [],
            "validation_status": invoice["validation_status"],
            "open_issues": _open_issues(repo, invoice["id"]),
        }

    with repo.transaction() as tx:
        tx.update_invoice_fields(invoice["id"], {p.column: p.value for p in effective})
        for p in effective:
            tx.record_review_action(
                invoice_id=invoice["id"],
                action="correct",
                user=reviewer,
                field=p.field,
                old_value=_display(p.column, current.get(p.column)),
                new_value=p.display,
                note=_note(note),
            )
        if any(p.field in SUNAT_RELEVANT_FIELDS for p in effective):
            tx.clear_sunat_status(invoice["id"])

        updated_row = _require_invoice(tx, invoice["id"])
        updated = InvoiceData.from_mapping(updated_row)
        # La moneda o la fecha pueden haber cambiado: se vuelve a buscar el tipo de cambio.
        rate = lookup_exchange_rate(tx, updated)
        tx.set_invoice_exchange_rate(invoice["id"], rate)
        _, issues = validate_invoice(
            updated,
            tolerance_cents=deps.config.amount_tolerance_cents,
            today=deps.today,
            exchange_rate=rate,
        )
        key = business_key(updated)
        if key:
            tx.lock_business_key(key)
        tx.set_business_key(invoice["id"], key)
        matches = tx.find_by_business_key(key, updated_row["document_id"]) if key else []
        if matches:
            issues.append(
                NewIssue(
                    "POSSIBLE_DUPLICATE",
                    Severity.WARNING,
                    "Existe otro documento con el mismo emisor, tipo, serie y número",
                    None,
                    "; ".join(f"#{m['document_id']} {m['file_name']}" for m in matches),
                )
            )
        tx.replace_rule_issues(invoice["id"], issues, is_preserved_on_revalidation)
        status = tx.recompute_validation_status(invoice["id"])
        open_issues = _open_issues(tx, invoice["id"])
    return {
        "invoice_id": invoice["id"],
        "changed": [
            {"field": p.field, "from": _display(p.column, current.get(p.column)), "to": p.display}
            for p in effective
        ],
        "validation_status": status,
        "open_issues": open_issues,
    }


def get_review_queue(deps: Deps) -> dict[str, Any]:
    """Facturas con errores o advertencias, documentos fallidos y pendientes de OCR."""
    repo = deps.repository
    invoices = []
    for row in repo.list_invoices_needing_review():
        row["issues"] = [
            i
            for i in repo.get_issues(row["invoice_id"])
            if i["severity"] != "info" and not i["resolved_at"]
        ]
        invoices.append(row)
    return {
        "invoices": invoices,
        "failed": repo.list_documents_by_status([DocumentStatus.FAILED]),
        "needs_ocr": repo.list_documents_by_status([DocumentStatus.NEEDS_OCR]),
    }
