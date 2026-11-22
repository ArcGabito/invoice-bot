"""Cumplimiento tributario de compras: SIRE, detracciones, retenciones y alertas de plazos."""

from __future__ import annotations

import csv
import io
import json
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import exists, or_, select
from sqlalchemy.orm import selectinload

from invoicebot.application import events
from invoicebot.domain.tax import (
    RetentionCase,
    SpotPayment,
    Voucher,
    annotation_deadline,
    detraction_deposit_amount,
    document_key,
    map_proposal_header,
    reconcile_sire,
    retention_cents,
    spot_payment_file,
)
from invoicebot.errors import UserError
from invoicebot.infrastructure.db.invoice_repository import month_range
from invoicebot.infrastructure.db.models import (
    Company,
    DetractionDeposit,
    Invoice,
    SireEntry,
    SireProposal,
    SupplierTaxStatus,
    utcnow,
)
from invoicebot.money import to_cents

if TYPE_CHECKING:
    from invoicebot.context import AppContext

#: Comprobantes que se anotan en el Registro de Compras.
PURCHASE_TYPES = ("01", "03", "07", "08", "14", "91")


def check_period(value: str) -> str:
    """ "2026-10" o "202610" → "202610"."""
    digits = value.replace("-", "")
    if len(digits) != 6 or not digits.isdigit() or not 1 <= int(digits[4:]) <= 12:
        raise UserError(f"Periodo no válido: {value!r} (usa AAAA-MM)")
    return digits


def _company_id(ctx: AppContext, ruc: str | None) -> int | None:
    if not ruc:
        return None
    from invoicebot.application.companies import require_company

    return int(require_company(ctx.companies, ruc)["id"])


# --- SIRE --------------------------------------------------------------------------------


def _parse_date(value: str) -> date | None:
    from invoicebot.infrastructure.sunat import parse_date

    parsed = parse_date(value.strip()) if value and value.strip() else None
    return date.fromisoformat(parsed) if parsed else None


def _read_rows(path: Path) -> list[list[str]]:
    data = path.read_bytes()
    if path.suffix.lower() == ".zip":
        import zipfile

        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = [n for n in archive.namelist() if n.lower().endswith((".txt", ".csv"))]
            if not names:
                raise UserError("El ZIP no contiene un TXT o CSV de la propuesta")
            info = archive.getinfo(names[0])
            if info.file_size > 200 * 1024 * 1024:
                raise UserError("El archivo de la propuesta supera 200 MB")
            data = archive.read(names[0])
    text = data.decode("utf-8-sig", errors="replace")
    if "�" in text:
        text = data.decode("latin-1")
    first = text.splitlines()[0] if text.strip() else ""
    delimiter = (
        "|"
        if first.count("|") >= 3
        else ("\t" if "\t" in first else (";" if first.count(";") > first.count(",") else ","))
    )
    return [
        row
        for row in csv.reader(io.StringIO(text), delimiter=delimiter)
        if any(c.strip() for c in row)
    ]


def import_sire_proposal(
    ctx: AppContext, path: Path, *, period: str, company_ruc: str | None, created_by: str | None
) -> dict[str, Any]:
    """Importa la propuesta del Registro de Compras (RCE) descargada del SIRE (TXT, CSV o ZIP)."""
    period = check_period(period)
    rows = _read_rows(path)
    if len(rows) < 2:
        raise UserError("La propuesta está vacía")
    try:
        columns = map_proposal_header(rows[0])
    except ValueError as error:
        raise UserError(str(error)) from error

    def cell(row: list[str], name: str) -> str:
        index = columns.get(name)
        return row[index].strip() if index is not None and index < len(row) else ""

    company_id = _company_id(ctx, company_ruc)
    with ctx.db.session() as session:
        proposal = SireProposal(
            company_id=company_id,
            period=period,
            source="file",
            file_name=path.name[:260],
            created_by=created_by,
            created_at=utcnow(),
        )
        for row in rows[1:]:
            number = cell(row, "number")
            if not number:
                continue
            proposal.entries.append(
                SireEntry(
                    supplier_ruc=cell(row, "supplier_ruc") or None,
                    document_type=cell(row, "document_type").zfill(2) or None,
                    series=cell(row, "series").upper() or None,
                    number=number,
                    issue_date=_parse_date(cell(row, "issue_date")),
                    currency=(cell(row, "currency") or "PEN").upper()[:3],
                    total_cents=to_cents(cell(row, "total")),
                    igv_cents=to_cents(cell(row, "igv")) if "igv" in columns else None,
                    car=cell(row, "car") or None,
                    raw=json.dumps(row, ensure_ascii=False),
                )
            )
        proposal.row_count = len(proposal.entries)
        session.add(proposal)
        session.flush()
        return {"proposal_id": proposal.id, "period": period, "rows": proposal.row_count}


def _voucher(invoice: Invoice) -> Voucher:
    return Voucher(
        invoice.supplier_ruc,
        invoice.document_type,
        invoice.series,
        invoice.number,
        invoice.issue_date,
        invoice.total_cents,
        invoice.igv_cents,
        invoice.currency,
        ref=invoice.id,
    )


def reconcile(ctx: AppContext, *, period: str, company_ruc: str | None = None) -> dict[str, Any]:
    """Cruza la última propuesta importada del periodo con los comprobantes de InvoiceBot."""
    period = check_period(period)
    company_id = _company_id(ctx, company_ruc)
    with ctx.db.session() as session:
        proposal = session.scalars(
            select(SireProposal)
            .where(
                SireProposal.period == period,
                SireProposal.company_id.is_(None)
                if company_id is None
                else SireProposal.company_id == company_id,
            )
            .order_by(SireProposal.id.desc())
            .options(selectinload(SireProposal.entries))
        ).first()
        if proposal is None:
            raise UserError(
                f"No hay una propuesta del SIRE importada para {period[:4]}-{period[4:]}"
            )
        sunat = [
            Voucher(
                e.supplier_ruc,
                e.document_type,
                e.series,
                e.number,
                e.issue_date,
                e.total_cents,
                e.igv_cents,
                e.currency,
                ref=e.id,
            )
            for e in proposal.entries
        ]
        keys = {v.key for v in sunat}
        start, end = month_range(f"{period[:4]}-{period[4:]}")
        query = select(Invoice).where(
            Invoice.review_status != "rejected",
            Invoice.document_type.in_(PURCHASE_TYPES),
            or_(
                Invoice.issue_date.between(start, end - timedelta(days=1)),
                Invoice.supplier_ruc.in_({k[0] for k in keys} or {""}),
            ),
        )
        if company_id is not None:
            query = query.where(Invoice.company_id == company_id)
        candidates = session.scalars(query).all()
        # Del mismo mes, todas; de otros meses, solo las que SUNAT pone en esta propuesta.
        registered = [
            _voucher(i)
            for i in candidates
            if (i.issue_date and start <= i.issue_date < end) or _voucher(i).key in keys
        ]
        result = reconcile_sire(sunat, registered)
        data = {
            "period": period,
            "proposal_id": proposal.id,
            "imported_at": proposal.created_at,
            "summary": result.summary(),
            "differences": [
                {
                    "kind": d.kind,
                    "message": d.message,
                    "supplier_ruc": d.key[0],
                    "document_type": d.key[1],
                    "series": d.key[2],
                    "number": d.key[3],
                    "sunat_total_cents": d.sunat.total_cents if d.sunat else None,
                    "bot_total_cents": d.bot.total_cents if d.bot else None,
                    "sunat_issue_date": d.sunat.issue_date if d.sunat else None,
                    "bot_issue_date": d.bot.issue_date if d.bot else None,
                    "invoice_id": d.bot.ref if d.bot else None,
                }
                for d in result.differences
            ],
        }
    return data


def reconciliation_xlsx(data: dict[str, Any]) -> bytes:
    from openpyxl import Workbook

    labels = {
        "only_sunat": "Solo en SUNAT",
        "only_bot": "Solo en InvoiceBot",
        "amount": "Total distinto",
        "igv": "IGV distinto",
        "date": "Fecha distinta",
    }
    book = Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.title = f"SIRE {data['period']}"
    sheet.append(
        [
            "Diferencia",
            "RUC proveedor",
            "Tipo",
            "Serie",
            "Número",
            "Total SUNAT",
            "Total InvoiceBot",
            "Fecha SUNAT",
            "Fecha InvoiceBot",
            "Factura #",
            "Detalle",
        ]
    )
    for d in data["differences"]:
        sheet.append(
            [
                labels.get(d["kind"], d["kind"]),
                d["supplier_ruc"],
                d["document_type"],
                d["series"],
                d["number"],
                d["sunat_total_cents"] / 100 if d["sunat_total_cents"] is not None else None,
                d["bot_total_cents"] / 100 if d["bot_total_cents"] is not None else None,
                d["sunat_issue_date"],
                d["bot_issue_date"],
                d["invoice_id"],
                d["message"],
            ]
        )
    summary = book.create_sheet("Resumen")
    for key, value in data["summary"].items():
        summary.append([labels.get(key, "Coinciden" if key == "matched" else key), value])
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


# --- Detracciones ------------------------------------------------------------------------


def pending_detractions(
    ctx: AppContext, *, company_ids: tuple[int, ...] | None = None
) -> list[dict[str, Any]]:
    """Facturas sujetas a detracción, autorizadas para pago y sin constancia de depósito."""
    with ctx.db.session() as session:
        query = (
            select(Invoice)
            .where(
                Invoice.detraction_amount_cents > 0,
                Invoice.review_status != "rejected",
                ~exists().where(DetractionDeposit.invoice_id == Invoice.id),
            )
            .order_by(Invoice.issue_date, Invoice.id)
        )
        if company_ids is not None:
            query = query.where(Invoice.company_id.in_(company_ids or (-1,)))
        return [
            {
                "invoice_id": i.id,
                "invoice": i.label,
                "supplier_ruc": i.supplier_ruc,
                "supplier_name": i.supplier_name,
                "issue_date": i.issue_date,
                "detraction_code": i.detraction_code,
                "detraction_percent": float(i.detraction_percent)
                if i.detraction_percent is not None
                else None,
                "account": i.detraction_account,
                "amount_cents": i.detraction_amount_cents,
                "deposit_cents": detraction_deposit_amount(i.detraction_amount_cents),
                "approval_status": i.approval_status,
                "company_id": i.company_id,
            }
            for i in session.scalars(query)
        ]


def record_deposit(
    ctx: AppContext,
    *,
    invoice_id: int,
    constancia: str,
    deposit_date: date,
    amount_cents: int,
    created_by: str | None,
    source: str = "manual",
) -> dict[str, Any]:
    constancia = constancia.strip()
    if not constancia.isdigit():
        raise UserError("El número de constancia de depósito es numérico")
    with ctx.db.session() as session:
        invoice = session.get(Invoice, invoice_id)
        if invoice is None:
            raise UserError(f"No existe la factura #{invoice_id}")
        if not invoice.detraction_amount_cents:
            raise UserError(f"La factura {invoice.label} no está sujeta a detracción")
        expected = detraction_deposit_amount(invoice.detraction_amount_cents)
        if amount_cents < expected:
            raise UserError(
                f"El depósito ({amount_cents / 100:.2f}) es menor que la detracción ({expected / 100:.2f})"
            )
        if session.scalar(
            select(DetractionDeposit.id).where(DetractionDeposit.constancia == constancia)
        ):
            raise UserError(f"La constancia {constancia} ya está registrada")
        deposit = DetractionDeposit(
            invoice_id=invoice_id,
            constancia=constancia,
            deposit_date=deposit_date,
            amount_cents=amount_cents,
            source=source,
            created_by=created_by,
            created_at=utcnow(),
        )
        session.add(deposit)
        session.flush()
        return {"id": deposit.id, "invoice_id": invoice_id, "constancia": constancia}


def detraction_payment_file(
    ctx: AppContext, *, invoice_ids: list[int], lot: str, company_ruc: str | None
) -> str:
    """Archivo de depósito masivo para las detracciones elegidas (pendientes)."""
    company_id = _company_id(ctx, company_ruc)
    with ctx.db.session() as session:
        if company_id is not None:
            company = session.get(Company, company_id)
            assert company is not None
            buyer_ruc, buyer_name = company.ruc, company.name
        else:
            buyer_ruc = ctx.config.sunat.ruc or ""
            buyer_name = ctx.config.env.get("INVOICEBOT_COMPANY_NAME", "")
        if len(buyer_ruc) != 11:
            raise UserError("Indica --company o define INVOICEBOT_SUNAT_RUC (RUC del adquirente)")
        pending = {p["invoice_id"]: p for p in pending_detractions(ctx)}
        payments = []
        for invoice_id in invoice_ids:
            item = pending.get(invoice_id)
            if item is None:
                raise UserError(f"La factura #{invoice_id} no tiene una detracción pendiente")
            if not (item["account"] and item["detraction_code"]):
                raise UserError(
                    f"La factura {item['invoice']} no tiene cuenta del Banco de la Nación o código"
                )
            invoice = session.get(Invoice, invoice_id)
            assert invoice is not None and invoice.issue_date is not None
            payments.append(
                SpotPayment(
                    supplier_ruc=item["supplier_ruc"],
                    supplier_name=item["supplier_name"] or "",
                    account=item["account"],
                    service_code=item["detraction_code"],
                    amount_cents=item["amount_cents"],
                    period=invoice.issue_date.strftime("%Y%m"),
                    document_type=invoice.document_type or "01",
                    series=invoice.series or "",
                    number=invoice.number or "",
                )
            )
    try:
        return spot_payment_file(
            buyer_ruc=buyer_ruc, buyer_name=buyer_name, lot=lot, payments=payments
        )
    except ValueError as error:
        raise UserError(str(error)) from error


# --- Retenciones ---------------------------------------------------------------------------


def set_supplier_status(
    ctx: AppContext,
    ruc: str,
    *,
    good_taxpayer: bool | None = None,
    retention_agent: bool | None = None,
    perception_agent: bool | None = None,
    source: str = "manual",
) -> dict[str, Any]:
    from invoicebot.application.companies import check_ruc

    ruc = check_ruc(ruc)
    with ctx.db.session() as session:
        status = session.get(SupplierTaxStatus, ruc) or SupplierTaxStatus(ruc=ruc)
        if good_taxpayer is not None:
            status.good_taxpayer = good_taxpayer
        if retention_agent is not None:
            status.retention_agent = retention_agent
        if perception_agent is not None:
            status.perception_agent = perception_agent
        status.source, status.updated_at = source, utcnow()
        session.add(status)
        return {
            "ruc": ruc,
            "good_taxpayer": status.good_taxpayer,
            "retention_agent": status.retention_agent,
            "perception_agent": status.perception_agent,
        }


def import_padron(ctx: AppContext, path: Path, *, kind: str) -> dict[str, int]:
    """Padrón de SUNAT (buenos contribuyentes, agentes de retención o de percepción): una
    línea por contribuyente con el RUC al inicio (TXT con | o CSV)."""
    flag = {
        "good": "good_taxpayer",
        "retention": "retention_agent",
        "perception": "perception_agent",
    }.get(kind)
    if flag is None:
        raise UserError("El padrón es good | retention | perception")
    count = 0
    for row in _read_rows(path):
        ruc = row[0].strip() if row else ""
        if len(ruc) == 11 and ruc.isdigit():
            set_supplier_status(ctx, ruc, source=f"padron:{kind}", **{flag: True})
            count += 1
    return {"imported": count}


def retention_report(
    ctx: AppContext, *, month: str, company_ruc: str | None
) -> list[dict[str, Any]]:
    """Retenciones del IGV de las facturas autorizadas para pago del mes (por fecha de emisión)."""
    start, end = month_range(month)
    company_id = _company_id(ctx, company_ruc)
    with ctx.db.session() as session:
        query = select(Invoice).where(
            Invoice.issue_date >= start,
            Invoice.issue_date < end,
            Invoice.approval_status.in_(("approved", "not_required")),
        )
        if company_id is not None:
            query = query.where(Invoice.company_id == company_id)
        rows = []
        for invoice in session.scalars(query.order_by(Invoice.issue_date, Invoice.id)):
            company = session.get(Company, invoice.company_id) if invoice.company_id else None
            status = session.get(SupplierTaxStatus, invoice.supplier_ruc or "")
            amount, reason = retention_cents(
                RetentionCase(
                    buyer_is_agent=bool(company and company.retention_agent),
                    supplier_is_agent=bool(status and status.retention_agent),
                    supplier_good_taxpayer=bool(status and status.good_taxpayer),
                    supplier_perception_agent=bool(status and status.perception_agent),
                    subject_to_detraction=bool(invoice.detraction_amount_cents),
                    document_type=invoice.document_type,
                    total_pen_cents=invoice.amount_pen_cents(),
                )
            )
            rows.append(
                {
                    "invoice_id": invoice.id,
                    "invoice": invoice.label,
                    "supplier_ruc": invoice.supplier_ruc,
                    "total_pen_cents": invoice.amount_pen_cents(),
                    "retention_cents": amount,
                    "net_payable_cents": invoice.amount_pen_cents() - amount,
                    "reason": reason,
                }
            )
        return rows


# --- Alertas -------------------------------------------------------------------------------


def compliance_alerts(
    ctx: AppContext, *, today: date | None = None, warn_days: int = 60
) -> dict[str, Any]:
    """Comprobantes cerca de perder el crédito fiscal y detracciones pendientes."""
    today = today or date.fromisoformat(ctx.config.today())
    alerts = {"annotation": 0, "detraction": 0}
    with ctx.db.session() as session:
        annotated = select(
            SireEntry.supplier_ruc, SireEntry.document_type, SireEntry.series, SireEntry.number
        )
        annotated_keys = {document_key(*row) for row in session.execute(annotated)}
        horizon = today - timedelta(days=365 - warn_days)
        risky = session.scalars(
            select(Invoice).where(
                Invoice.review_status != "rejected",
                Invoice.document_type.in_(PURCHASE_TYPES),
                Invoice.issue_date <= horizon,
                Invoice.issue_date > today - timedelta(days=400),
            )
        ).all()
        for invoice in risky:
            if _voucher(invoice).key in annotated_keys or invoice.issue_date is None:
                continue
            deadline = annotation_deadline(invoice.issue_date)
            if deadline < today:
                continue
            events.publish(
                session,
                "tax.annotation_deadline",
                {
                    "invoice_id": invoice.id,
                    "invoice": invoice.label,
                    "supplier_ruc": invoice.supplier_ruc,
                    "issue_date": invoice.issue_date,
                    "deadline": deadline,
                    "days_left": (deadline - today).days,
                },
                subject=f"invoice/{invoice.id}",
                company_id=invoice.company_id,
            )
            alerts["annotation"] += 1
    for item in pending_detractions(ctx):
        if item["approval_status"] in ("approved", "not_required"):
            with ctx.db.session() as session:
                events.publish(
                    session,
                    "detraction.pending",
                    {key: value for key, value in item.items() if key != "company_id"},
                    subject=f"invoice/{item['invoice_id']}",
                    company_id=item["company_id"],
                )
            alerts["detraction"] += 1
    events.dispatch(ctx)
    return alerts
