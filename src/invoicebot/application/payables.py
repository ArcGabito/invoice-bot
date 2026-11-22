"""Contabilidad (asiento de provisión) y cuentas por pagar (programación y pagos).

- El asiento sale de cada factura autorizada para pago: ``journal(month)`` y sus
  exportaciones (CSV, Excel y una plantilla tipo Concar).
- Al autorizarse una factura (``approved``/``not_required``) se programa su pago:
  fecha de vencimiento (o la condición por defecto) e importe neto a pagar (total menos
  retención y detracción). El evento ``invoice.payment_scheduled`` lo recibe bank-bot
  como partida por pagar; ``GET /api/v1/payables`` sirve para consultarlas.
- bank-bot (o tesorería) confirma el pago con ``POST /api/v1/payments``: la factura pasa
  a ``partial`` o ``paid`` y se publica ``invoice.paid``.
"""

from __future__ import annotations

import csv
import io
from datetime import date
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from invoicebot.application import events
from invoicebot.domain.accounting import (
    Accounts,
    EntryInvoice,
    JournalLine,
    is_balanced,
    payment_date,
    provision_entry,
)
from invoicebot.domain.tax import RetentionCase, detraction_deposit_amount, retention_cents
from invoicebot.errors import UserError
from invoicebot.infrastructure.db.invoice_repository import month_range
from invoicebot.infrastructure.db.models import (
    Company,
    CostCenter,
    Document,
    Invoice,
    Payment,
    SupplierTaxStatus,
    utcnow,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from invoicebot.context import AppContext

PAYABLE = ("approved", "not_required")


def accounts(ctx: AppContext) -> Accounts:
    env = ctx.config.env
    defaults = Accounts()
    return Accounts(
        payable=env.get("INVOICEBOT_ACCOUNT_PAYABLE") or defaults.payable,
        igv=env.get("INVOICEBOT_ACCOUNT_IGV") or defaults.igv,
        expense=env.get("INVOICEBOT_ACCOUNT_EXPENSE") or defaults.expense,
        other_taxes=env.get("INVOICEBOT_ACCOUNT_OTHER_TAXES") or defaults.other_taxes,
        destination_credit=env.get("INVOICEBOT_ACCOUNT_DESTINATION_CREDIT")
        or defaults.destination_credit,
    )


def supplier_email(ctx: AppContext, invoice: Invoice, session: Session) -> str | None:
    """Correo desde el que llegó el comprobante (para avisos de email-bot)."""
    sha = session.scalar(select(Document.sha256).where(Document.id == invoice.document_id))
    origins = ctx.repository.find_mail_origins(sha) if sha else []
    sender = origins[-1]["sender"] if origins else None
    if sender and "<" in sender:
        sender = sender.split("<", 1)[1].rstrip(">")
    return sender or None


def invoice_event_data(ctx: AppContext, invoice: Invoice, session: Session) -> dict[str, Any]:
    return {
        "invoice_id": invoice.id,
        "invoice": invoice.label,
        "document_type": invoice.document_type,
        "supplier_ruc": invoice.supplier_ruc,
        "supplier_name": invoice.supplier_name,
        "supplier_email": supplier_email(ctx, invoice, session),
        "issue_date": invoice.issue_date,
        "currency": invoice.currency,
        "total_cents": invoice.total_cents,
    }


# --- Eventos del ciclo de vida -------------------------------------------------------------


def publish_received(ctx: AppContext, invoice_id: int) -> None:
    with ctx.db.session() as session:
        invoice = session.get(Invoice, invoice_id)
        if invoice is None:
            return
        events.publish(
            session,
            "invoice.received",
            {
                **invoice_event_data(ctx, invoice, session),
                "validation_status": invoice.validation_status,
            },
            subject=f"invoice/{invoice_id}",
            company_id=invoice.company_id,
        )
    events.dispatch(ctx)


def publish_rejected(ctx: AppContext, invoice_id: int, reason: str | None) -> None:
    with ctx.db.session() as session:
        invoice = session.get(Invoice, invoice_id)
        if invoice is None:
            return
        events.publish(
            session,
            "invoice.rejected",
            {**invoice_event_data(ctx, invoice, session), "reason": reason},
            subject=f"invoice/{invoice_id}",
            company_id=invoice.company_id,
        )
    events.dispatch(ctx)


# --- Programación de pagos -----------------------------------------------------------------


def _net_payable(session: Session, invoice: Invoice) -> tuple[int, int, int]:
    """(neto a pagar al proveedor, retención, detracción) en la moneda del comprobante."""
    total = int(invoice.total_cents or 0)
    detraction = detraction_deposit_amount(invoice.detraction_amount_cents)
    if detraction and invoice.currency != "PEN" and invoice.exchange_rate:
        # La detracción se deposita en soles: se descuenta convertida a la moneda de la factura.
        detraction = round(detraction / float(invoice.exchange_rate))
    company = session.get(Company, invoice.company_id) if invoice.company_id else None
    status = session.get(SupplierTaxStatus, invoice.supplier_ruc or "")
    retention, _ = retention_cents(
        RetentionCase(
            buyer_is_agent=bool(company and company.retention_agent),
            supplier_is_agent=bool(status and status.retention_agent),
            supplier_good_taxpayer=bool(status and status.good_taxpayer),
            supplier_perception_agent=bool(status and status.perception_agent),
            subject_to_detraction=bool(detraction),
            document_type=invoice.document_type,
            total_pen_cents=invoice.amount_pen_cents(),
        )
    )
    if retention and invoice.currency != "PEN" and invoice.exchange_rate:
        retention = round(retention / float(invoice.exchange_rate))
    return total - retention - detraction, retention, detraction


def schedule_payment(ctx: AppContext, invoice_id: int) -> dict[str, Any]:
    terms = int(ctx.config.env.get("INVOICEBOT_PAYMENT_TERMS_DAYS") or 30)
    with ctx.db.session() as session:
        invoice = session.get(Invoice, invoice_id)
        if invoice is None:
            raise UserError(f"No existe la factura #{invoice_id}")
        if invoice.approval_status not in PAYABLE:
            return {"invoice_id": invoice_id, "skipped": "la factura no está autorizada para pago"}
        if invoice.document_type == "07":
            return {
                "invoice_id": invoice_id,
                "skipped": "una nota de crédito reduce el saldo de su factura",
            }
        if invoice.payment_status in ("scheduled", "partial", "paid"):
            return {"invoice_id": invoice_id, "payment_status": invoice.payment_status}
        if invoice.issue_date is None:
            raise UserError(f"La factura {invoice.label} no tiene fecha de emisión")
        net, retention, detraction = _net_payable(session, invoice)
        # Las notas de crédito aplicadas reducen lo que se paga.
        credits = session.scalar(
            select(func.coalesce(func.sum(Invoice.total_cents), 0)).where(
                Invoice.applied_to_invoice_id == invoice_id,
                Invoice.document_type == "07",
                Invoice.review_status != "rejected",
            )
        )
        net -= int(credits or 0)
        invoice.scheduled_payment_date = payment_date(invoice.issue_date, invoice.due_date, terms)
        invoice.payable_cents = max(0, net)
        invoice.paid_cents = invoice.paid_cents or 0
        invoice.payment_status = "scheduled"
        data = {
            **invoice_event_data(ctx, invoice, session),
            "due_date": invoice.due_date,
            "scheduled_payment_date": invoice.scheduled_payment_date,
            "payable_cents": invoice.payable_cents,
            "retention_cents": retention,
            "detraction_cents": detraction,
            "credit_notes_cents": int(credits or 0),
            "company_id": invoice.company_id,
        }
        events.publish(
            session,
            "invoice.payment_scheduled",
            data,
            subject=f"invoice/{invoice_id}",
            company_id=invoice.company_id,
        )
    events.dispatch(ctx)
    return {
        "invoice_id": invoice_id,
        "payment_status": "scheduled",
        **{k: data[k] for k in ("scheduled_payment_date", "payable_cents")},
    }


def payables(
    ctx: AppContext,
    *,
    status: str | None = "scheduled",
    until: date | None = None,
    company_ids: tuple[int, ...] | None = None,
) -> list[dict[str, Any]]:
    with ctx.db.session() as session:
        query = (
            select(Invoice)
            .where(Invoice.payment_status.is_not(None))
            .order_by(Invoice.scheduled_payment_date, Invoice.id)
        )
        if status:
            query = query.where(Invoice.payment_status == status)
        if until:
            query = query.where(Invoice.scheduled_payment_date <= until)
        if company_ids is not None:
            query = query.where(Invoice.company_id.in_(company_ids or (-1,)))
        return [
            {
                "invoice_id": i.id,
                "invoice": i.label,
                "supplier_ruc": i.supplier_ruc,
                "supplier_name": i.supplier_name,
                "currency": i.currency,
                "total_cents": i.total_cents,
                "payable_cents": i.payable_cents,
                "paid_cents": i.paid_cents or 0,
                "pending_cents": (i.payable_cents or 0) - (i.paid_cents or 0),
                "scheduled_payment_date": i.scheduled_payment_date,
                "due_date": i.due_date,
                "payment_status": i.payment_status,
                "company_id": i.company_id,
            }
            for i in session.scalars(query)
        ]


def record_payment(
    ctx: AppContext,
    *,
    invoice_id: int | None = None,
    supplier_ruc: str | None = None,
    series: str | None = None,
    number: str | None = None,
    amount_cents: int,
    paid_on: date,
    reference: str | None,
    source: str,
    created_by: str | None,
) -> dict[str, Any]:
    """Pago confirmado (por id o por proveedor, serie y número). Idempotente por referencia."""
    if amount_cents <= 0:
        raise UserError("El importe pagado debe ser positivo")
    with ctx.db.session() as session:
        invoice = session.get(Invoice, invoice_id) if invoice_id else None
        if invoice is None and supplier_ruc and series and number:
            invoice = session.scalars(
                select(Invoice).where(
                    Invoice.supplier_ruc == supplier_ruc,
                    Invoice.series == series.upper(),
                    Invoice.number.in_({number, number.lstrip("0") or "0", number.zfill(8)}),
                    Invoice.document_type != "07",
                )
            ).first()
        if invoice is None:
            raise UserError("No se encontró la factura del pago")
        if invoice.payment_status is None:
            raise UserError(f"La factura {invoice.label} no tiene un pago programado")
        if reference:
            duplicate = session.scalar(
                select(Payment.id).where(
                    Payment.invoice_id == invoice.id, Payment.reference == reference
                )
            )
            if duplicate:
                return {
                    "invoice_id": invoice.id,
                    "payment_status": invoice.payment_status,
                    "duplicate": True,
                }
        session.add(
            Payment(
                invoice_id=invoice.id,
                amount_cents=amount_cents,
                paid_on=paid_on,
                reference=reference,
                source=source,
                created_by=created_by,
                created_at=utcnow(),
            )
        )
        try:
            session.flush()
        except IntegrityError as error:
            raise UserError("Ese pago ya está registrado") from error
        invoice.paid_cents = int(invoice.paid_cents or 0) + amount_cents
        invoice.payment_status = (
            "paid" if invoice.paid_cents >= int(invoice.payable_cents or 0) else "partial"
        )
        events.publish(
            session,
            "invoice.paid",
            {
                **invoice_event_data(ctx, invoice, session),
                "payment_status": invoice.payment_status,
                "paid_cents": invoice.paid_cents,
                "payable_cents": invoice.payable_cents,
                "paid_on": paid_on,
                "reference": reference,
            },
            subject=f"invoice/{invoice.id}",
            company_id=invoice.company_id,
        )
        result = {
            "invoice_id": invoice.id,
            "payment_status": invoice.payment_status,
            "paid_cents": invoice.paid_cents,
        }
    events.dispatch(ctx)
    return result


# --- Contabilidad ------------------------------------------------------------------------------


def journal(
    ctx: AppContext, *, month: str, company_ids: tuple[int, ...] | None = None
) -> list[JournalLine]:
    """Asientos de provisión de las facturas autorizadas del mes (por fecha de emisión)."""
    start, end = month_range(month)
    chart = accounts(ctx)
    with ctx.db.session() as session:
        destinations = {
            c.code: c.destination_account
            for c in session.scalars(select(CostCenter).where(CostCenter.active == True))  # noqa: E712
        }
        query = (
            select(Invoice)
            .where(
                Invoice.issue_date >= start,
                Invoice.issue_date < end,
                Invoice.approval_status.in_(PAYABLE),
            )
            .order_by(Invoice.issue_date, Invoice.id)
        )
        if company_ids is not None:
            query = query.where(Invoice.company_id.in_(company_ids or (-1,)))
        lines: list[JournalLine] = []
        for invoice in session.scalars(query):
            if invoice.issue_date is None or invoice.total_cents is None:
                continue
            other = (
                int(invoice.isc_cents or 0)
                + int(invoice.icbper_cents or 0)
                + int(invoice.other_taxes_cents or 0)
            )
            entry = provision_entry(
                EntryInvoice(
                    invoice_id=invoice.id,
                    issue_date=invoice.issue_date,
                    due_date=invoice.due_date,
                    supplier_ruc=invoice.supplier_ruc or "",
                    supplier_name=invoice.supplier_name or "",
                    document_type=invoice.document_type or "01",
                    series=invoice.series or "",
                    number=invoice.number or "",
                    currency=invoice.currency or "PEN",
                    exchange_rate=float(invoice.exchange_rate) if invoice.exchange_rate else None,
                    total_cents=int(invoice.total_cents),
                    igv_cents=int(invoice.igv_cents or 0),
                    other_taxes_cents=other,
                    account_code=invoice.account_code,
                    cost_center=invoice.cost_center,
                    destination_account=destinations.get(invoice.cost_center or ""),
                ),
                chart,
            )
            if not is_balanced(entry):
                raise UserError(f"El asiento de {invoice.label} no cuadra")
            lines.extend(entry)
        return lines


def _money(cents: int | None) -> str:
    return "" if cents is None else f"{cents / 100:.2f}"


def journal_csv(lines: list[JournalLine]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "fecha",
            "cuenta",
            "debe",
            "haber",
            "moneda",
            "importe_original",
            "tipo_cambio",
            "ruc",
            "tipo",
            "serie",
            "numero",
            "vencimiento",
            "centro_costo",
            "glosa",
            "factura_id",
        ]
    )
    for line in lines:
        writer.writerow(
            [
                line.entry_date.isoformat(),
                line.account,
                _money(line.debit_cents),
                _money(line.credit_cents),
                line.currency,
                _money(line.original_cents),
                line.exchange_rate or "",
                line.supplier_ruc,
                line.document_type,
                line.series,
                line.number,
                line.due_date.isoformat() if line.due_date else "",
                line.cost_center or "",
                line.description,
                line.invoice_id,
            ]
        )
    return buffer.getvalue()


#: Columnas de la plantilla de importación de asientos de Concar (revísalas con tu versión).
CONCAR_COLUMNS = (
    "Sub Diario",
    "Número de Comprobante",
    "Fecha de Comprobante",
    "Código de Moneda",
    "Glosa Principal",
    "Tipo de Cambio",
    "Tipo de Conversión",
    "Flag de Conversión de Moneda",
    "Fecha Tipo de Cambio",
    "Cuenta Contable",
    "Código de Anexo",
    "Código de Centro de Costo",
    "Debe / Haber",
    "Importe Original",
    "Importe en Dólares",
    "Importe en Soles",
    "Tipo de Documento",
    "Número de Documento",
    "Fecha de Documento",
    "Fecha de Vencimiento",
    "Código de Area",
    "Glosa Detalle",
)


def journal_xlsx(lines: list[JournalLine], *, concar: bool = False, subdiary: str = "11") -> bytes:
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    assert sheet is not None
    if not concar:
        sheet.title = "Asientos"
        reader = csv.reader(io.StringIO(journal_csv(lines)))
        for row in reader:
            sheet.append(row)
    else:
        sheet.title = "Concar"
        sheet.append(list(CONCAR_COLUMNS))
        voucher: dict[int, int] = {}
        for line in lines:
            number = voucher.setdefault(line.invoice_id, len(voucher) + 1)
            pen = line.debit_cents or line.credit_cents
            original = line.original_cents if line.original_cents is not None else pen
            sheet.append(
                [
                    subdiary,
                    f"{line.entry_date:%m}{number:04d}",
                    line.entry_date.strftime("%d/%m/%Y"),
                    "MN" if line.currency == "PEN" else "US",
                    line.description[:40],
                    line.exchange_rate or "",
                    "V",
                    "S",
                    line.entry_date.strftime("%d/%m/%Y"),
                    line.account,
                    line.supplier_ruc,
                    line.cost_center or "",
                    "D" if line.debit_cents else "H",
                    original / 100,
                    (original / 100) if line.currency != "PEN" else "",
                    pen / 100,
                    {"01": "FT", "03": "BV", "07": "NA", "08": "ND"}.get(
                        line.document_type, line.document_type
                    ),
                    f"{line.series}-{line.number}",
                    line.entry_date.strftime("%d/%m/%Y"),
                    line.due_date.strftime("%d/%m/%Y") if line.due_date else "",
                    "",
                    line.description[:30],
                ]
            )
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def add_cost_center(
    ctx: AppContext, *, code: str, name: str, destination_account: str | None
) -> dict[str, Any]:
    with ctx.db.session() as session:
        center = session.get(CostCenter, code) or CostCenter(code=code)
        center.name, center.destination_account, center.active = name, destination_account, True
        session.add(center)
        return {"code": code, "name": name, "destination_account": destination_account}
