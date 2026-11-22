"""Cuentas por pagar: órdenes de compra, recepciones, match y datos contables.

Flujo de una factura guardada (tarea ``purchasing.match``):

1. Nota de crédito o débito: se aplica a su factura (referencia del XML) y termina.
2. Se busca su orden de compra: la que cita el XML (``OrderReference``), la de la guía
   que cita (``DespatchDocumentReference``) o, si el proveedor tiene una sola orden abierta
   con saldo suficiente, esa.
3. Match a 2 o 3 vías con tolerancia (``INVOICEBOT_MATCH_TOLERANCE_*``).
4. Centro de costo de la orden y cuenta contable aprendida del proveedor.
5. Si coincide, la validación no tiene problemas e ``INVOICEBOT_AUTO_APPROVE_MATCHED``
   está activo, se aprueba la revisión sola; con la revisión aprobada empieza la
   aprobación de pago (``approvals.start``).
"""

from __future__ import annotations

import csv
import io
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from invoicebot.application import events
from invoicebot.domain.purchasing import InvoiceFacts, MatchTolerance, OrderFacts, match_invoice
from invoicebot.errors import UserError
from invoicebot.infrastructure.db.models import (
    GoodsReceipt,
    GoodsReceiptLine,
    Invoice,
    InvoiceMatch,
    PurchaseOrder,
    PurchaseOrderLine,
    SupplierAccounting,
    utcnow,
)
from invoicebot.money import to_cents

if TYPE_CHECKING:
    from invoicebot.context import AppContext

NOTE_TYPES = ("07", "08")


# --- Importación -------------------------------------------------------------------------


def _company_id(ctx: AppContext, ruc: str | None) -> int | None:
    if not ruc:
        return None
    from invoicebot.application.companies import require_company

    return int(require_company(ctx.companies, ruc)["id"])


def _cents(value: Any, field: str) -> int:
    cents = to_cents(value)
    if cents is None:
        raise UserError(f"{field}: importe no válido ({value!r})")
    return int(cents)


def _quantity(value: Any) -> Decimal:
    try:
        quantity = Decimal(str(value).replace(",", "."))
    except InvalidOperation as error:
        raise UserError(f"Cantidad no válida: {value!r}") from error
    if quantity <= 0:
        raise UserError(f"La cantidad debe ser positiva: {value!r}")
    return quantity


def _date(value: Any) -> date | None:
    if not value:
        return None
    from invoicebot.infrastructure.sunat import parse_date

    parsed = parse_date(str(value))
    if not parsed:
        raise UserError(f"Fecha no válida: {value!r}")
    return date.fromisoformat(parsed)


def upsert_purchase_order(ctx: AppContext, data: dict[str, Any], *, source: str) -> dict[str, Any]:
    """Crea o reemplaza una orden de compra (por empresa y número) con sus líneas.

    ``data``: po_number, supplier_ruc, currency, issue_date, cost_center, requester,
    requires_receipt, company_ruc y lines [{line_no, item_code, description, quantity,
    unit_price}]. Los importes van sin IGV.
    """
    from invoicebot.application.companies import check_ruc

    number = str(data.get("po_number") or "").strip()
    if not number:
        raise UserError("Falta po_number")
    lines = data.get("lines") or []
    if not lines:
        raise UserError(f"La orden {number} no tiene líneas")
    company_id = _company_id(ctx, data.get("company_ruc"))
    supplier = check_ruc(data.get("supplier_ruc"))
    with ctx.db.session() as session:
        order = session.scalar(
            select(PurchaseOrder)
            .where(PurchaseOrder.po_number == number)
            .where(
                PurchaseOrder.company_id.is_(None)
                if company_id is None
                else PurchaseOrder.company_id == company_id
            )
            .options(selectinload(PurchaseOrder.lines))
        )
        created = order is None
        if order is None:
            order = PurchaseOrder(po_number=number, company_id=company_id, created_at=utcnow())
            session.add(order)
        order.supplier_ruc = supplier
        order.supplier_name = data.get("supplier_name")
        order.currency = str(data.get("currency") or "PEN").upper()
        order.issue_date = _date(data.get("issue_date"))
        order.cost_center = data.get("cost_center")
        order.requester = data.get("requester")
        order.requires_receipt = bool(data.get("requires_receipt", True))
        order.status = str(data.get("status") or order.status or "open")
        order.source = source
        order.updated_at = utcnow()
        existing = {line.line_no: line for line in order.lines}
        seen: set[int] = set()
        for index, raw in enumerate(lines, start=1):
            line_no = int(raw.get("line_no") or index)
            line = existing.get(line_no) or PurchaseOrderLine(line_no=line_no)
            line.item_code = raw.get("item_code")
            line.description = raw.get("description")
            line.quantity = _quantity(raw.get("quantity"))
            line.unit_price_cents = _cents(raw.get("unit_price"), "unit_price")
            if line_no not in existing:
                order.lines.append(line)
            seen.add(line_no)
        for line_no, line in existing.items():
            if line_no not in seen:
                order.lines.remove(line)
        order.total_cents = sum(line.amount_cents for line in order.lines)
        session.flush()
        result = _order_dict(order)
    result["created"] = created
    return result


def record_goods_receipt(ctx: AppContext, data: dict[str, Any], *, source: str) -> dict[str, Any]:
    """Registra una recepción (nota de ingreso, guía de remisión o conformidad de servicio).

    ``data``: receipt_number, supplier_ruc, receipt_date, po_number, company_ruc,
    despatch_reference (serie-número de la guía), amount (sin IGV, si no hay líneas) y
    lines [{line_no (de la orden), quantity}].
    """
    from invoicebot.application.companies import check_ruc

    number = str(data.get("receipt_number") or "").strip()
    if not number:
        raise UserError("Falta receipt_number")
    company_id = _company_id(ctx, data.get("company_ruc"))
    supplier = check_ruc(data.get("supplier_ruc"))
    with ctx.db.session() as session:
        order = None
        if data.get("po_number"):
            order = _find_order(session, company_id, str(data["po_number"]).strip())
            if order is None:
                raise UserError(f"No existe la orden de compra {data['po_number']}")
        receipt = session.scalar(
            select(GoodsReceipt).where(
                GoodsReceipt.receipt_number == number,
                GoodsReceipt.supplier_ruc == supplier,
                GoodsReceipt.company_id.is_(None)
                if company_id is None
                else GoodsReceipt.company_id == company_id,
            )
        )
        if receipt is not None:
            raise UserError(f"La recepción {number} de {supplier} ya está registrada")
        receipt = GoodsReceipt(
            company_id=company_id,
            receipt_number=number,
            supplier_ruc=supplier,
            receipt_date=_date(data.get("receipt_date")),
            despatch_reference=(data.get("despatch_reference") or None),
            amount_cents=_cents(data["amount"], "amount") if data.get("amount") else None,
            order=order,
            source=source,
            created_at=utcnow(),
        )
        by_line = {line.line_no: line for line in (order.lines if order else [])}
        for raw in data.get("lines") or []:
            po_line = by_line.get(int(raw.get("line_no") or 0))
            if order is not None and po_line is None:
                raise UserError(
                    f"La orden {order.po_number} no tiene la línea {raw.get('line_no')}"
                )
            receipt.lines.append(
                GoodsReceiptLine(
                    order_line=po_line,
                    item_code=raw.get("item_code") or (po_line.item_code if po_line else None),
                    quantity=_quantity(raw.get("quantity")),
                )
            )
        session.add(receipt)
        session.flush()
        return {
            "id": receipt.id,
            "receipt_number": receipt.receipt_number,
            "order_id": receipt.order_id,
            "received_cents": _receipt_value(receipt),
        }


def _read_csv(path: Path) -> list[dict[str, str]]:
    text = path.read_text(encoding="utf-8-sig")
    dialect = csv.Sniffer().sniff(text.splitlines()[0]) if text.strip() else csv.excel
    return [
        {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
        for row in csv.DictReader(io.StringIO(text), dialect=dialect)
    ]


def import_orders_csv(ctx: AppContext, path: Path) -> dict[str, Any]:
    """Una fila por línea: company_ruc, po_number, supplier_ruc, currency, issue_date,
    cost_center, line_no, item_code, description, quantity, unit_price."""
    groups: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in _read_csv(path):
        groups[(row.get("company_ruc", ""), row.get("po_number", ""))].append(row)
    created = updated = 0
    for (company, number), rows in groups.items():
        head = rows[0]
        result = upsert_purchase_order(
            ctx,
            {
                **head,
                "company_ruc": company or None,
                "po_number": number,
                "requires_receipt": head.get("requires_receipt", "true").lower() != "false",
                "lines": rows,
            },
            source="csv",
        )
        created, updated = created + result["created"], updated + (not result["created"])
    return {"orders": len(groups), "created": created, "updated": updated}


def import_receipts_csv(ctx: AppContext, path: Path) -> dict[str, Any]:
    """Una fila por línea: company_ruc, receipt_number, supplier_ruc, receipt_date,
    po_number, despatch_reference, line_no, quantity (o amount sin líneas)."""
    groups: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in _read_csv(path):
        key = (
            row.get("company_ruc", ""),
            row.get("supplier_ruc", ""),
            row.get("receipt_number", ""),
        )
        groups[key].append(row)
    count = 0
    for (company, _supplier, _number), rows in groups.items():
        head = rows[0]
        lines = [r for r in rows if r.get("line_no") and r.get("quantity")]
        record_goods_receipt(
            ctx,
            {**head, "company_ruc": company or None, "lines": lines},
            source="csv",
        )
        count += 1
    return {"receipts": count}


# --- Match --------------------------------------------------------------------------------


def _company_filter(column: Any, company_id: int | None) -> Any:
    """Una orden (o recepción) sin empresa sirve para cualquiera; con empresa, solo para esa."""
    if company_id is None:
        return column.is_(None)
    return or_(column == company_id, column.is_(None))


def _find_order(session: Session, company_id: int | None, number: str) -> PurchaseOrder | None:
    query = (
        select(PurchaseOrder)
        .where(
            PurchaseOrder.po_number == number, _company_filter(PurchaseOrder.company_id, company_id)
        )
        # Primero la de la empresa; después la general.
        .order_by(PurchaseOrder.company_id.desc())
    )
    return session.scalars(query.options(selectinload(PurchaseOrder.lines))).first()


def _receipt_value(receipt: GoodsReceipt) -> int:
    """Valor recibido sin IGV: cantidades × precio de la orden, o el importe declarado."""
    priced = [(line.quantity, line.order_line) for line in receipt.lines]
    if priced and all(po_line is not None for _, po_line in priced):
        return sum(
            round(float(quantity) * po_line.unit_price_cents)
            for quantity, po_line in priced
            if po_line is not None
        )
    return int(receipt.amount_cents or 0)


def _order_facts(session: Session, order: PurchaseOrder, invoice_id: int) -> OrderFacts:
    receipts = session.scalars(
        select(GoodsReceipt)
        .where(GoodsReceipt.order_id == order.id)
        .options(selectinload(GoodsReceipt.lines).selectinload(GoodsReceiptLine.order_line))
    ).all()
    invoiced = session.scalar(
        select(func.coalesce(func.sum(InvoiceMatch.matched_cents), 0))
        .join(Invoice, Invoice.id == InvoiceMatch.invoice_id)
        .where(
            InvoiceMatch.order_id == order.id,
            InvoiceMatch.invoice_id != invoice_id,
            InvoiceMatch.status == "matched",
            Invoice.review_status != "rejected",
        )
    )
    return OrderFacts(
        po_number=order.po_number,
        supplier_ruc=order.supplier_ruc,
        currency=order.currency,
        status=order.status,
        requires_receipt=order.requires_receipt,
        ordered_cents=int(order.total_cents),
        received_cents=sum(_receipt_value(r) for r in receipts),
        invoiced_cents=int(invoiced or 0),
    )


def invoice_references(ctx: AppContext, document_id: int) -> dict[str, list[str]]:
    """Referencias del XML del comprobante (vacías si vino en PDF o imagen)."""
    from invoicebot.infrastructure.documents.scanner import XML_MIME_TYPE
    from invoicebot.infrastructure.documents.ubl import parse_references

    document = ctx.repository.get_document(document_id)
    empty: dict[str, list[str]] = {"orders": [], "despatches": [], "billing": []}
    if document is None or document["mime_type"] != XML_MIME_TYPE:
        return empty
    for path in [
        document["file_path"],
        *(o["file_path"] for o in ctx.repository.get_occurrences(document_id)),
    ]:
        try:
            return parse_references(Path(path).read_bytes())
        except OSError:
            continue
    return empty


@dataclass
class _Located:
    order: PurchaseOrder | None
    found_by: str | None


def _locate_order(
    session: Session, invoice: Invoice, refs: dict[str, list[str]], tolerance: MatchTolerance
) -> _Located:
    for number in refs["orders"]:
        order = _find_order(session, invoice.company_id, number)
        if order is not None:
            return _Located(order, "xml_order")
    for despatch in refs["despatches"]:
        receipt = session.scalars(
            select(GoodsReceipt).where(
                GoodsReceipt.supplier_ruc == invoice.supplier_ruc,
                GoodsReceipt.despatch_reference == despatch,
                GoodsReceipt.order_id.is_not(None),
            )
        ).first()
        if receipt is not None and receipt.order is not None:
            return _Located(receipt.order, "xml_despatch")
    candidates = session.scalars(
        select(PurchaseOrder).where(
            PurchaseOrder.supplier_ruc == invoice.supplier_ruc,
            PurchaseOrder.status == "open",
            PurchaseOrder.currency == (invoice.currency or "PEN"),
            _company_filter(PurchaseOrder.company_id, invoice.company_id),
        )
    ).all()
    fitting = []
    for order in candidates:
        facts = _order_facts(session, order, invoice.id)
        pending = facts.ordered_cents - facts.invoiced_cents
        if pending + tolerance.allowed(order.total_cents) >= invoice.net_cents:
            fitting.append(order)
    if len(fitting) == 1:
        return _Located(fitting[0], "supplier_amount")
    return _Located(None, None)


def _suggest_accounting(session: Session, invoice: Invoice, order: PurchaseOrder | None) -> None:
    if invoice.accounting_source == "manual":
        return
    if order is not None and order.cost_center and not invoice.cost_center:
        invoice.cost_center, invoice.accounting_source = order.cost_center, "order"
    if invoice.account_code or not invoice.supplier_ruc:
        return
    learned = session.scalars(
        select(SupplierAccounting)
        .where(
            SupplierAccounting.company_key == (invoice.company_id or 0),
            SupplierAccounting.supplier_ruc == invoice.supplier_ruc,
        )
        .order_by(SupplierAccounting.uses.desc(), SupplierAccounting.last_used_at.desc())
    ).first()
    if learned is not None:
        invoice.account_code = learned.account_code
        invoice.cost_center = invoice.cost_center or learned.cost_center
        invoice.accounting_source = invoice.accounting_source or "history"


def _apply_note(ctx: AppContext, session: Session, note: Invoice) -> dict[str, Any]:
    refs = invoice_references(ctx, note.document_id)["billing"]
    for reference in refs:
        series, _, number = reference.partition("-")
        target = session.scalars(
            select(Invoice).where(
                Invoice.supplier_ruc == note.supplier_ruc,
                Invoice.series == series,
                Invoice.number.in_({number, number.lstrip("0") or "0", number.zfill(8)}),
                Invoice.document_type.not_in(NOTE_TYPES),
            )
        ).first()
        if target is not None:
            note.applied_to_invoice_id = target.id
            events.publish(
                session,
                "invoice.note_applied",
                {
                    "note_id": note.id,
                    "note": note.label,
                    "note_type": note.document_type,
                    "invoice_id": target.id,
                    "invoice": target.label,
                    "amount_cents": note.total_cents,
                },
                subject=f"invoice/{target.id}",
                company_id=target.company_id,
            )
            return {"applied_to": target.id, "reference": reference}
    return {"applied_to": None, "references": refs}


def run_match(ctx: AppContext, invoice_id: int, *, po_number: str | None = None) -> dict[str, Any]:
    """Match de una factura (idempotente: se puede repetir al llegar la orden o la guía)."""
    settings = ctx.config.purchasing
    tolerance = MatchTolerance(settings.tolerance_percent, settings.tolerance_cents)
    with ctx.db.session() as session:
        invoice = session.get(Invoice, invoice_id)
        if invoice is None:
            raise UserError(f"No existe la factura #{invoice_id}")
        if invoice.document_type in NOTE_TYPES:
            note_result = _apply_note(ctx, session, invoice)
        else:
            note_result = None
    if note_result is not None:
        events.dispatch(ctx)
        return {"invoice_id": invoice_id, "note": True, **note_result}
    with ctx.db.session() as session:
        invoice = session.get(Invoice, invoice_id)
        assert invoice is not None
        if po_number:
            order = _find_order(session, invoice.company_id, po_number)
            if order is None:
                raise UserError(f"No existe la orden de compra {po_number}")
            located = _Located(order, "manual")
        else:
            located = _locate_order(
                session, invoice, invoice_references(ctx, invoice.document_id), tolerance
            )
        exceptions: list[dict[str, Any]] = []
        status, match_type = "no_order", None
        if located.order is not None:
            outcome = match_invoice(
                InvoiceFacts(invoice.supplier_ruc, invoice.currency, invoice.net_cents),
                _order_facts(session, located.order, invoice_id),
                tolerance,
            )
            status, match_type = outcome.status, outcome.match_type
            exceptions = [e.as_dict() for e in outcome.exceptions]
        # La fila se completa antes de entrar en la sesión (el autoflush no la guarda a medias).
        match = session.get(InvoiceMatch, invoice_id) or InvoiceMatch(invoice_id=invoice_id)
        match.status, match.match_type = status, match_type
        match.order_id = located.order.id if located.order else None
        match.found_by = located.found_by
        match.matched_cents = invoice.net_cents
        match.exceptions = json.dumps(exceptions, ensure_ascii=False) if exceptions else None
        match.checked_at = utcnow()
        session.add(match)
        if located.order is not None:
            events.publish(
                session,
                "invoice.matched" if status == "matched" else "invoice.match_exception",
                {
                    "invoice_id": invoice_id,
                    "invoice": invoice.label,
                    "supplier_ruc": invoice.supplier_ruc,
                    "po_number": located.order.po_number,
                    "match_type": match_type,
                    "exceptions": exceptions,
                },
                subject=f"invoice/{invoice_id}",
                company_id=invoice.company_id,
            )
        _suggest_accounting(session, invoice, located.order)
        validation, review = invoice.validation_status, invoice.review_status
        po = located.order.po_number if located.order else None
    events.dispatch(ctx)
    auto = False
    if (
        status == "matched"
        and settings.auto_approve_matched
        and validation == "valid"
        and review == "pending"
    ):
        from invoicebot.application.review import approve_invoice

        approve_invoice(
            ctx.review_deps(),
            invoice_id,
            user="invoicebot:match",
            note=f"Coincide con la orden de compra {po}",
        )
        review, auto = "approved", True
    if review == "approved":
        ctx.after_review_approved(invoice_id)
    return {
        "invoice_id": invoice_id,
        "status": status,
        "po_number": po,
        "found_by": located.found_by,
        "exceptions": exceptions,
        "auto_approved_review": auto,
    }


def get_match(ctx: AppContext, invoice_id: int) -> dict[str, Any] | None:
    with ctx.db.session() as session:
        # La factura pasa por la Row-Level Security: si no es visible, no hay match.
        if session.get(Invoice, invoice_id) is None:
            return None
        match = session.get(InvoiceMatch, invoice_id)
        if match is None:
            return None
        order = session.get(PurchaseOrder, match.order_id) if match.order_id else None
        return {
            "invoice_id": invoice_id,
            "status": match.status,
            "match_type": match.match_type,
            "found_by": match.found_by,
            "matched_cents": match.matched_cents,
            "exceptions": json.loads(match.exceptions) if match.exceptions else [],
            "checked_at": match.checked_at,
            "order": _order_dict(order) if order else None,
        }


def set_accounting(
    ctx: AppContext,
    invoice_id: int,
    *,
    account_code: str | None,
    cost_center: str | None,
) -> dict[str, Any]:
    """Asigna cuenta y centro de costo a mano; se aprende para el proveedor."""
    with ctx.db.session() as session:
        invoice = session.get(Invoice, invoice_id)
        if invoice is None:
            raise UserError(f"No existe la factura #{invoice_id}")
        invoice.account_code = (account_code or "").strip() or None
        invoice.cost_center = (cost_center or "").strip() or None
        invoice.accounting_source = "manual"
        if invoice.supplier_ruc and (invoice.account_code or invoice.cost_center):
            key = invoice.company_id or 0
            learned = session.scalars(
                select(SupplierAccounting).where(
                    SupplierAccounting.company_key == key,
                    SupplierAccounting.supplier_ruc == invoice.supplier_ruc,
                    SupplierAccounting.account_code == invoice.account_code,
                    SupplierAccounting.cost_center == invoice.cost_center
                    if invoice.cost_center
                    else SupplierAccounting.cost_center.is_(None),
                )
            ).first()
            if learned is None:
                session.add(
                    SupplierAccounting(
                        company_key=key,
                        supplier_ruc=invoice.supplier_ruc,
                        account_code=invoice.account_code,
                        cost_center=invoice.cost_center,
                        uses=1,
                        last_used_at=utcnow(),
                    )
                )
            else:
                learned.uses += 1
                learned.last_used_at = utcnow()
        return {
            "invoice_id": invoice_id,
            "account_code": invoice.account_code,
            "cost_center": invoice.cost_center,
        }


def list_orders(
    ctx: AppContext,
    *,
    company_ids: tuple[int, ...] | None,
    supplier_ruc: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    with ctx.db.session() as session:
        query = select(PurchaseOrder).options(selectinload(PurchaseOrder.lines))
        if company_ids is not None:
            query = query.where(PurchaseOrder.company_id.in_(company_ids or (-1,)))
        if supplier_ruc:
            query = query.where(PurchaseOrder.supplier_ruc == supplier_ruc)
        if status:
            query = query.where(PurchaseOrder.status == status)
        orders = session.scalars(query.order_by(PurchaseOrder.id.desc()).limit(limit)).all()
        return [_order_summary(session, order) for order in orders]


def get_order(
    ctx: AppContext, order_id: int, *, company_ids: tuple[int, ...] | None
) -> dict[str, Any] | None:
    with ctx.db.session() as session:
        order = session.get(PurchaseOrder, order_id)
        if order is None or (company_ids is not None and order.company_id not in company_ids):
            return None
        summary = _order_summary(session, order)
        summary["invoices"] = [
            {"invoice_id": m.invoice_id, "status": m.status, "matched_cents": m.matched_cents}
            for m in session.scalars(select(InvoiceMatch).where(InvoiceMatch.order_id == order_id))
        ]
        return summary


def _order_dict(order: PurchaseOrder) -> dict[str, Any]:
    return {
        "id": order.id,
        "company_id": order.company_id,
        "po_number": order.po_number,
        "supplier_ruc": order.supplier_ruc,
        "supplier_name": order.supplier_name,
        "currency": order.currency,
        "issue_date": order.issue_date,
        "total_cents": order.total_cents,
        "status": order.status,
        "requires_receipt": order.requires_receipt,
        "cost_center": order.cost_center,
        "lines": [
            {
                "line_no": line.line_no,
                "item_code": line.item_code,
                "description": line.description,
                "quantity": float(line.quantity),
                "unit_price_cents": line.unit_price_cents,
                "amount_cents": line.amount_cents,
            }
            for line in order.lines
        ],
    }


def _order_summary(session: Session, order: PurchaseOrder) -> dict[str, Any]:
    facts = _order_facts(session, order, invoice_id=0)
    return {
        **_order_dict(order),
        "received_cents": facts.received_cents,
        "invoiced_cents": facts.invoiced_cents,
        "pending_cents": facts.ordered_cents - facts.invoiced_cents,
    }


def invoices_waiting(ctx: AppContext, order_id: int) -> list[int]:
    """Facturas de una orden que no coincidieron (p. ej. esperaban la recepción)."""
    with ctx.db.session() as session:
        return [
            int(i)
            for i in session.scalars(
                select(InvoiceMatch.invoice_id).where(
                    InvoiceMatch.order_id == order_id, InvoiceMatch.status == "exception"
                )
            )
        ]
