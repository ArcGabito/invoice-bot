"""Antifraude, portal de proveedores e indicadores de cuentas por pagar."""

from __future__ import annotations

import hashlib
import secrets
import statistics
from contextlib import suppress
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from invoicebot.application import events
from invoicebot.domain.fraud import FRAUD_PREFIX, PastInvoice, detect
from invoicebot.domain.invoice import NewIssue, Severity
from invoicebot.errors import UserError
from invoicebot.infrastructure.db.invoice_repository import month_range
from invoicebot.infrastructure.db.models import (
    V_INVOICE_LIFECYCLE,
    Invoice,
    PortalToken,
    PortalUpload,
    ReviewAction,
    SupplierContact,
    utcnow,
)

if TYPE_CHECKING:
    from invoicebot.context import AppContext

LINK_MINUTES = 30
SESSION_HOURS = 8


# --- Antifraude ---------------------------------------------------------------------------


def check_fraud(ctx: AppContext, invoice_id: int) -> list[str]:
    """Agrega (o renueva) las advertencias FRAUD_* de una factura. Devuelve sus códigos."""
    settings = ctx.config.env
    threshold = round(float(settings.get("INVOICEBOT_FRAUD_NEW_SUPPLIER_AMOUNT") or 10_000) * 100)
    with ctx.db.session() as session:
        invoice = session.get(Invoice, invoice_id)
        if invoice is None or not invoice.supplier_ruc or invoice.document_type == "07":
            return []
        history = [
            PastInvoice(p.id, p.series, p.number, p.issue_date, p.amount_pen_cents())
            for p in session.scalars(
                select(Invoice).where(
                    Invoice.supplier_ruc == invoice.supplier_ruc,
                    Invoice.id != invoice_id,
                    Invoice.review_status != "rejected",
                    Invoice.document_type != "07",
                )
            )
        ]
        signals = detect(
            series=invoice.series,
            number=invoice.number,
            issue_date=invoice.issue_date,
            total_pen_cents=invoice.amount_pen_cents(),
            history=history,
            new_supplier_threshold_cents=threshold,
        )
        company_id, label, supplier = invoice.company_id, invoice.label, invoice.supplier_ruc
    repo = ctx.repository
    with repo.transaction() as tx:
        tx.replace_rule_issues(
            invoice_id,
            [NewIssue(s.code, Severity.WARNING, s.message, s.expected, s.actual) for s in signals],
            lambda code: not code.startswith(FRAUD_PREFIX),
        )
        tx.recompute_validation_status(invoice_id)
    if signals:
        with ctx.db.session() as session:
            events.publish(
                session,
                "invoice.fraud_alert",
                {
                    "invoice_id": invoice_id,
                    "invoice": label,
                    "supplier_ruc": supplier,
                    "signals": [{"code": s.code, "message": s.message} for s in signals],
                },
                subject=f"invoice/{invoice_id}",
                company_id=company_id,
            )
        events.dispatch(ctx)
    return [s.code for s in signals]


# --- Portal de proveedores ---------------------------------------------------------------------


def _hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def add_contact(
    ctx: AppContext, *, supplier_ruc: str, email: str, source: str = "manual"
) -> dict[str, Any]:
    from invoicebot.application.companies import check_ruc

    ruc, address = check_ruc(supplier_ruc), email.strip().lower()
    if "@" not in address or len(address) > 254:
        raise UserError(f"Correo no válido: {email!r}")
    with ctx.db.session() as session:
        exists = session.scalar(
            select(SupplierContact.id).where(
                SupplierContact.supplier_ruc == ruc, SupplierContact.email == address
            )
        )
        if exists is None:
            session.add(
                SupplierContact(supplier_ruc=ruc, email=address, source=source, created_at=utcnow())
            )
    return {"supplier_ruc": ruc, "email": address}


def learn_contact(ctx: AppContext, invoice_id: int) -> None:
    """El correo desde el que llegó un comprobante queda como contacto del proveedor."""
    from invoicebot.application.payables import supplier_email

    with ctx.db.session() as session:
        invoice = session.get(Invoice, invoice_id)
        email = supplier_email(ctx, invoice, session) if invoice and invoice.supplier_ruc else None
        ruc = invoice.supplier_ruc if invoice else None
    if email and ruc:
        with suppress(UserError, IntegrityError):
            add_contact(ctx, supplier_ruc=ruc, email=email, source="mail")


def request_login(ctx: AppContext, *, supplier_ruc: str, email: str) -> bool:
    """Envía (con el evento `portal.login_link`) un enlace de acceso si el correo es un
    contacto del proveedor. La respuesta al navegador es la misma en los dos casos."""
    address = email.strip().lower()
    with ctx.db.session() as session:
        known = session.scalar(
            select(SupplierContact.id).where(
                SupplierContact.supplier_ruc == supplier_ruc.strip(),
                SupplierContact.email == address,
                SupplierContact.active == True,  # noqa: E712
            )
        )
        if known is None:
            return False
        token = secrets.token_urlsafe(32)
        session.add(
            PortalToken(
                token_hash=_hash(token),
                kind="link",
                supplier_ruc=supplier_ruc.strip(),
                email=address,
                expires_at=utcnow() + timedelta(minutes=LINK_MINUTES),
                created_at=utcnow(),
            )
        )
        events.publish(
            session,
            "portal.login_link",
            {
                "supplier_ruc": supplier_ruc.strip(),
                "email": address,
                "login_url": ctx.public_url(f"/portal/login/{token}"),
                "expires_minutes": LINK_MINUTES,
            },
            subject=f"supplier/{supplier_ruc.strip()}",
        )
    events.dispatch(ctx)
    return True


def open_session(ctx: AppContext, link_token: str) -> tuple[str, str] | None:
    """Canjea el enlace (un solo uso) por una sesión. Devuelve (token de sesión, RUC)."""
    if not link_token or len(link_token) > 100:
        return None
    now = utcnow()
    with ctx.db.session() as session:
        link = session.get(PortalToken, _hash(link_token))
        if link is None or link.kind != "link" or link.used_at is not None or link.expires_at < now:
            return None
        link.used_at = now
        token = secrets.token_urlsafe(32)
        session.add(
            PortalToken(
                token_hash=_hash(token),
                kind="session",
                supplier_ruc=link.supplier_ruc,
                email=link.email,
                expires_at=now + timedelta(hours=SESSION_HOURS),
                created_at=now,
            )
        )
        return token, link.supplier_ruc


def session_supplier(ctx: AppContext, token: str | None) -> tuple[str, str] | None:
    """(RUC, correo) de una sesión del portal vigente."""
    if not token or len(token) > 100:
        return None
    with ctx.db.session() as session:
        row = session.get(PortalToken, _hash(token))
        if row is None or row.kind != "session" or row.expires_at < utcnow():
            return None
        return row.supplier_ruc, row.email


def close_session(ctx: AppContext, token: str | None) -> None:
    if token:
        with ctx.db.session() as session:
            row = session.get(PortalToken, _hash(token))
            if row is not None:
                session.delete(row)


def supplier_invoices(
    ctx: AppContext, supplier_ruc: str, *, limit: int = 200
) -> list[dict[str, Any]]:
    """Lo que ve el proveedor: estado de cada comprobante y la fecha de pago, sin datos internos."""
    with ctx.db.session() as session:
        rows = session.scalars(
            select(Invoice)
            .where(Invoice.supplier_ruc == supplier_ruc)
            .order_by(Invoice.issue_date.desc(), Invoice.id.desc())
            .limit(limit)
        ).all()
        reasons = {
            r.invoice_id: r.note
            for r in session.scalars(
                select(ReviewAction).where(
                    ReviewAction.invoice_id.in_([i.id for i in rows] or [-1]),
                    ReviewAction.action == "reject",
                )
            )
        }
        items = []
        for invoice in rows:
            if invoice.review_status == "rejected":
                stage, detail = "rechazado", reasons.get(invoice.id)
            elif invoice.payment_status == "paid":
                stage, detail = "pagado", None
            elif invoice.payment_status in ("scheduled", "partial"):
                stage, detail = "pago programado", None
            elif invoice.approval_status == "pending":
                stage, detail = "en aprobación", None
            elif invoice.approval_status == "rejected":
                stage, detail = "observado", "Comunícate con el área de compras"
            else:
                stage, detail = "en revisión", None
            items.append(
                {
                    "invoice": invoice.label,
                    "document_type": invoice.document_type,
                    "issue_date": invoice.issue_date,
                    "currency": invoice.currency,
                    "total_cents": invoice.total_cents,
                    "stage": stage,
                    "detail": detail,
                    "scheduled_payment_date": invoice.scheduled_payment_date,
                    "payable_cents": invoice.payable_cents,
                    "paid_cents": invoice.paid_cents,
                }
            )
        return items


def record_upload(
    ctx: AppContext, *, supplier_ruc: str, email: str, folder: str, files: int, job_id: int | None
) -> None:
    with ctx.db.session() as session:
        session.add(
            PortalUpload(
                supplier_ruc=supplier_ruc,
                email=email,
                folder=folder[:1024],
                files=files,
                job_id=job_id,
                created_at=utcnow(),
            )
        )


# --- Indicadores -----------------------------------------------------------------------------


def _hours(start: datetime | date | None, end: datetime | date | None) -> float | None:
    if start is None or end is None:
        return None
    if isinstance(start, datetime) is not isinstance(end, datetime):
        start = (
            start if isinstance(start, datetime) else datetime.combine(start, datetime.min.time())
        )
        end = end if isinstance(end, datetime) else datetime.combine(end, datetime.min.time())
    return max(0.0, (end - start).total_seconds() / 3600)


def kpis(
    ctx: AppContext, *, month: str, company_ids: tuple[int, ...] | None = None
) -> dict[str, Any]:
    """Indicadores del mes (por fecha de recepción) a partir de `v_invoice_lifecycle`."""
    start, end = month_range(month)
    view = V_INVOICE_LIFECYCLE.c
    query = select(V_INVOICE_LIFECYCLE).where(
        view.received_at >= datetime.combine(start, datetime.min.time()),
        view.received_at < datetime.combine(end, datetime.min.time()),
    )
    if company_ids is not None:
        query = query.where(view.company_id.in_(company_ids or (-1,)))
    with ctx.db.connect() as conn:
        rows = [dict(r) for r in conn.execute(query).mappings()]
        suppliers = conn.execute(
            select(func.count(func.distinct(Invoice.supplier_ruc)))
        ).scalar_one()
    env = ctx.config.env
    price_in = float(env.get("INVOICEBOT_AI_PRICE_INPUT_PER_MTOK") or 0)
    price_out = float(env.get("INVOICEBOT_AI_PRICE_OUTPUT_PER_MTOK") or 0)
    authorized = [r for r in rows if r["authorized_at"]]
    to_authorize = [
        h for r in authorized if (h := _hours(r["received_at"], r["authorized_at"])) is not None
    ]
    to_pay = [
        h
        for r in rows
        if r["last_payment_on"]
        and (h := _hours(r["authorized_at"], r["last_payment_on"])) is not None
    ]
    tokens_in = sum(int(r["input_tokens"] or 0) for r in rows)
    tokens_out = sum(int(r["output_tokens"] or 0) for r in rows)
    ai_cost = tokens_in / 1e6 * price_in + tokens_out / 1e6 * price_out
    return {
        "month": month,
        "received": len(rows),
        "from_xml": sum(1 for r in rows if r["data_source"] == "xml"),
        "authorized": len(authorized),
        "paid": sum(1 for r in rows if r["payment_status"] == "paid"),
        "rejected": sum(1 for r in rows if r["review_status"] == "rejected"),
        "straight_through": sum(int(r["straight_through"] or 0) for r in rows),
        "straight_through_rate": round(
            sum(int(r["straight_through"] or 0) for r in rows) / len(rows), 4
        )
        if rows
        else None,
        "median_hours_to_authorize": round(statistics.median(to_authorize), 1)
        if to_authorize
        else None,
        "median_hours_authorize_to_pay": round(statistics.median(to_pay), 1) if to_pay else None,
        "sunat_observed": sum(
            1
            for r in rows
            if (r["sunat_status"] or "").upper() in ("ANULADO", "NO AUTORIZADO", "NO EXISTE")
        ),
        "ai_input_tokens": tokens_in,
        "ai_output_tokens": tokens_out,
        "ai_cost_usd": round(ai_cost, 4) if price_in or price_out else None,
        "ai_cost_per_invoice_usd": round(ai_cost / len(rows), 5)
        if rows and (price_in or price_out)
        else None,
        "suppliers": int(suppliers or 0),
    }
