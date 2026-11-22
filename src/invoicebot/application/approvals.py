"""Aprobación de pago con matriz por empresa, centro de costo, proveedor y monto.

- Empieza cuando la revisión de la factura queda aprobada (los datos son correctos).
- Niveles secuenciales: el nivel 2 recibe la solicitud cuando el 1 aprueba.
- Un nivel sin aprobador (regla automática) se aprueba solo si la factura coincide con su
  orden de compra; si no coincide, pasa a quien indique ``escalate_to``.
- Delegaciones por vacaciones o licencias (cadenas A → B → C).
- Cada solicitud lleva un enlace de un clic (token de un solo uso, 7 días) que se envía
  por correo con el evento ``approval.requested``.
- ``approvals.remind`` (programado) recuerda una vez lo vencido y después lo escala.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, true
from sqlalchemy.orm import Session

from invoicebot.application import events
from invoicebot.domain.purchasing import (
    Delegation,
    Rule,
    Step,
    approval_steps,
    effective_approver,
)
from invoicebot.errors import ReviewError, UserError
from invoicebot.infrastructure.db.models import (
    ApprovalDelegation,
    ApprovalRequest,
    ApprovalRule,
    Invoice,
    InvoiceMatch,
    utcnow,
)

if TYPE_CHECKING:
    from invoicebot.context import AppContext

TOKEN_DAYS = 7
SYSTEM_APPROVER = "invoicebot:match"
#: Estados de una factura que puede pagarse.
PAYABLE = ("approved", "not_required")


def _hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def _rules(session: Session) -> list[Rule]:
    return [
        Rule(
            id=r.id,
            level=r.level,
            approver=r.approver,
            company_id=r.company_id,
            cost_center=r.cost_center,
            supplier_ruc=r.supplier_ruc,
            min_cents=r.min_amount_cents,
            max_cents=r.max_amount_cents,
            sla_hours=r.sla_hours,
            escalate_to=r.escalate_to,
        )
        for r in session.scalars(select(ApprovalRule).where(ApprovalRule.active == true()))
    ]


def _delegations(session: Session, today: date) -> list[Delegation]:
    rows = session.scalars(
        select(ApprovalDelegation).where(
            ApprovalDelegation.starts_on <= today, ApprovalDelegation.ends_on >= today
        )
    )
    return [Delegation(d.username, d.delegate, d.starts_on, d.ends_on) for d in rows]


def _steps(session: Session, invoice: Invoice) -> list[Step]:
    return approval_steps(
        _rules(session),
        company_id=invoice.company_id,
        cost_center=invoice.cost_center,
        supplier_ruc=invoice.supplier_ruc,
        amount_pen_cents=invoice.amount_pen_cents(),
    )


def _invoice_data(invoice: Invoice) -> dict[str, Any]:
    return {
        "invoice_id": invoice.id,
        "invoice": invoice.label,
        "supplier_ruc": invoice.supplier_ruc,
        "supplier_name": invoice.supplier_name,
        "currency": invoice.currency,
        "total_cents": invoice.total_cents,
        "due_date": invoice.due_date,
        "cost_center": invoice.cost_center,
    }


def _open_request(
    ctx: AppContext, session: Session, invoice: Invoice, step: Step, approver: str, now: datetime
) -> ApprovalRequest:
    token = secrets.token_urlsafe(32)
    request = ApprovalRequest(
        invoice_id=invoice.id,
        level=step.level,
        rule_id=step.rule_id,
        approver=approver,
        status="pending",
        due_at=now + timedelta(hours=step.sla_hours),
        escalate_to=step.escalate_to,
        token_hash=_hash(token),
        token_expires_at=now + timedelta(days=TOKEN_DAYS),
        created_at=now,
    )
    session.add(request)
    session.flush()
    events.publish(
        session,
        "approval.requested",
        {
            **_invoice_data(invoice),
            "approval_id": request.id,
            "level": step.level,
            "approver": approver,
            "due_at": request.due_at,
            "approve_url": ctx.public_url(f"/approve/{token}"),
        },
        subject=f"invoice/{invoice.id}",
        company_id=invoice.company_id,
    )
    return request


def _advance(
    ctx: AppContext, session: Session, invoice: Invoice, *, after_level: int, now: datetime
) -> str:
    """Abre el siguiente nivel que necesita a una persona, o termina la aprobación."""
    match = session.get(InvoiceMatch, invoice.id)
    matched = match is not None and match.status == "matched"
    delegations = _delegations(session, now.date())
    for step in (s for s in _steps(session, invoice) if s.level > after_level):
        if step.approver is None and matched:
            session.add(
                ApprovalRequest(
                    invoice_id=invoice.id,
                    level=step.level,
                    rule_id=step.rule_id,
                    approver=SYSTEM_APPROVER,
                    status="approved",
                    due_at=now,
                    decided_at=now,
                    decided_by=SYSTEM_APPROVER,
                    comment="Coincide con la orden de compra",
                    created_at=now,
                )
            )
            continue
        person = step.approver or step.escalate_to
        if person is None:
            raise UserError(
                f"La regla automática #{step.rule_id} necesita escalate_to para las facturas "
                "que no coinciden con su orden de compra"
            )
        _open_request(
            ctx, session, invoice, step, effective_approver(person, delegations, now.date()), now
        )
        invoice.approval_status = "pending"
        return "pending"
    final = "approved" if after_level > 0 or _steps(session, invoice) else "not_required"
    invoice.approval_status = final
    events.publish(
        session,
        "invoice.approved_for_payment",
        {**_invoice_data(invoice), "approval_status": final},
        subject=f"invoice/{invoice.id}",
        company_id=invoice.company_id,
    )
    return final


def start(ctx: AppContext, invoice_id: int, *, restart: bool = False) -> dict[str, Any]:
    """Empieza la aprobación de pago de una factura con la revisión aprobada."""
    now = utcnow()
    with ctx.db.session() as session:
        invoice = session.get(Invoice, invoice_id)
        if invoice is None:
            raise UserError(f"No existe la factura #{invoice_id}")
        if invoice.review_status != "approved":
            return {"invoice_id": invoice_id, "skipped": "la revisión no está aprobada"}
        if invoice.document_type in ("07", "08"):
            return {"invoice_id": invoice_id, "skipped": "las notas siguen a su factura"}
        if invoice.approval_status is not None and not restart:
            return {"invoice_id": invoice_id, "approval_status": invoice.approval_status}
        for pending in session.scalars(
            select(ApprovalRequest).where(
                ApprovalRequest.invoice_id == invoice_id, ApprovalRequest.status == "pending"
            )
        ):
            pending.status, pending.token_hash = "cancelled", None
        status = _advance(ctx, session, invoice, after_level=0, now=now)
    events.dispatch(ctx)
    _after(ctx, invoice_id, status)
    return {"invoice_id": invoice_id, "approval_status": status}


def _after(ctx: AppContext, invoice_id: int, status: str) -> None:
    if status in PAYABLE:
        ctx.after_payment_authorized(invoice_id)


def _check_approver(
    request: ApprovalRequest, user: str, *, is_admin: bool, today: date, session: Session
) -> None:
    if is_admin or user == request.approver:
        return
    if effective_approver(request.approver, _delegations(session, today), today) == user:
        return
    raise ReviewError(f"La aprobación #{request.id} le corresponde a {request.approver}")


def _decide(
    ctx: AppContext,
    session: Session,
    request: ApprovalRequest,
    *,
    approve: bool,
    user: str,
    comment: str | None,
    now: datetime,
) -> dict[str, Any]:
    if request.status != "pending":
        raise ReviewError(f"La aprobación #{request.id} ya está {request.status}")
    if not approve and not (comment or "").strip():
        raise ReviewError("Indica el motivo del rechazo")
    invoice = session.get(Invoice, request.invoice_id)
    if invoice is None:
        raise ReviewError("La factura ya no existe o no es visible")
    request.status = "approved" if approve else "rejected"
    request.decided_at, request.decided_by = now, user
    request.comment = (comment or "").strip()[:1000] or None
    request.token_hash = None
    if approve:
        status = _advance(ctx, session, invoice, after_level=request.level, now=now)
    else:
        invoice.approval_status = status = "rejected"
        events.publish(
            session,
            "invoice.payment_rejected",
            {**_invoice_data(invoice), "by": user, "reason": request.comment},
            subject=f"invoice/{invoice.id}",
            company_id=invoice.company_id,
        )
    return {"approval_id": request.id, "invoice_id": invoice.id, "approval_status": status}


def decide(
    ctx: AppContext,
    approval_id: int,
    *,
    approve: bool,
    user: str,
    comment: str | None = None,
    is_admin: bool = False,
) -> dict[str, Any]:
    now = utcnow()
    with ctx.db.session() as session:
        request = session.get(ApprovalRequest, approval_id)
        # Sin la factura visible (Row-Level Security) tampoco se ve su aprobación.
        if request is None or session.get(Invoice, request.invoice_id) is None:
            raise ReviewError(f"No existe la aprobación #{approval_id}")
        _check_approver(request, user, is_admin=is_admin, today=now.date(), session=session)
        result = _decide(
            ctx, session, request, approve=approve, user=user, comment=comment, now=now
        )
    events.dispatch(ctx)
    _after(ctx, int(result["invoice_id"]), result["approval_status"])
    return result


def by_token(ctx: AppContext, token: str) -> dict[str, Any] | None:
    """La solicitud del enlace de un clic (para mostrarla), o None si no vale."""
    if not token or len(token) > 100:
        return None
    with ctx.db.session() as session:
        request = session.scalars(
            select(ApprovalRequest).where(ApprovalRequest.token_hash == _hash(token))
        ).first()
        if request is None or request.status != "pending":
            return None
        if request.token_expires_at is not None and request.token_expires_at < utcnow():
            return None
        invoice = session.get(Invoice, request.invoice_id)
        if invoice is None:
            return None
        return {
            **_invoice_data(invoice),
            "approval_id": request.id,
            "level": request.level,
            "approver": request.approver,
        }


def decide_by_token(
    ctx: AppContext, token: str, *, approve: bool, comment: str | None
) -> dict[str, Any]:
    now = utcnow()
    with ctx.db.session() as session:
        request = session.scalars(
            select(ApprovalRequest).where(ApprovalRequest.token_hash == _hash(token or ""))
        ).first()
        if (
            request is None
            or request.status != "pending"
            or (request.token_expires_at is not None and request.token_expires_at < now)
        ):
            raise ReviewError("El enlace ya se usó o caducó")
        result = _decide(
            ctx,
            session,
            request,
            approve=approve,
            user=f"{request.approver} (enlace)",
            comment=comment,
            now=now,
        )
    events.dispatch(ctx)
    _after(ctx, int(result["invoice_id"]), result["approval_status"])
    return result


def remind(ctx: AppContext, *, now: datetime | None = None) -> dict[str, Any]:
    """Recuerda una vez cada aprobación vencida y, si sigue vencida un día después, la escala."""
    now = now or utcnow()
    reminded = escalated = 0
    with ctx.db.session() as session:
        overdue = session.scalars(
            select(ApprovalRequest).where(
                ApprovalRequest.status == "pending", ApprovalRequest.due_at < now
            )
        ).all()
        delegations = _delegations(session, now.date())
        for request in overdue:
            invoice = session.get(Invoice, request.invoice_id)
            if invoice is None:
                continue
            recent = request.last_reminder_at and now - request.last_reminder_at < timedelta(
                hours=24
            )
            if request.reminders_sent >= 1 and request.escalate_to and not recent:
                request.status, request.token_hash = "escalated", None
                step = Step(request.level, request.escalate_to, request.rule_id or 0, 24, None)
                target = effective_approver(request.escalate_to, delegations, now.date())
                new = _open_request(ctx, session, invoice, step, target, now)
                events.publish(
                    session,
                    "approval.escalated",
                    {
                        **_invoice_data(invoice),
                        "approval_id": new.id,
                        "from": request.approver,
                        "to": target,
                        "level": request.level,
                    },
                    subject=f"invoice/{invoice.id}",
                    company_id=invoice.company_id,
                )
                escalated += 1
            elif not recent:
                # Nuevo enlace: el anterior pudo perderse entre los correos.
                token = secrets.token_urlsafe(32)
                request.token_hash = _hash(token)
                request.token_expires_at = now + timedelta(days=TOKEN_DAYS)
                request.reminders_sent += 1
                request.last_reminder_at = now
                events.publish(
                    session,
                    "approval.reminder",
                    {
                        **_invoice_data(invoice),
                        "approval_id": request.id,
                        "approver": request.approver,
                        "due_at": request.due_at,
                        "reminders_sent": request.reminders_sent,
                        "approve_url": ctx.public_url(f"/approve/{token}"),
                    },
                    subject=f"invoice/{invoice.id}",
                    company_id=invoice.company_id,
                )
                reminded += 1
    events.dispatch(ctx)
    return {"reminded": reminded, "escalated": escalated}


def list_requests(
    ctx: AppContext,
    *,
    status: str | None = "pending",
    approver: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    with ctx.db.session() as session:
        query = (
            select(ApprovalRequest, Invoice)
            .join(Invoice, Invoice.id == ApprovalRequest.invoice_id)
            .order_by(ApprovalRequest.due_at)
            .limit(limit)
        )
        if status:
            query = query.where(ApprovalRequest.status == status)
        if approver:
            query = query.where(ApprovalRequest.approver == approver)
        return [
            {
                "id": request.id,
                "level": request.level,
                "approver": request.approver,
                "status": request.status,
                "due_at": request.due_at,
                "overdue": request.status == "pending" and request.due_at < utcnow(),
                "decided_by": request.decided_by,
                "comment": request.comment,
                **_invoice_data(invoice),
            }
            for request, invoice in session.execute(query).all()
        ]


def add_rule(ctx: AppContext, **fields: Any) -> dict[str, Any]:
    if fields.get("approver") is None and not fields.get("escalate_to"):
        raise UserError(
            "Una regla automática (sin aprobador) necesita escalate_to: quién aprueba si la "
            "factura no coincide con su orden de compra"
        )
    if fields.get("max_amount_cents") is not None and fields["max_amount_cents"] <= fields.get(
        "min_amount_cents", 0
    ):
        raise UserError("El monto máximo debe ser mayor que el mínimo")
    with ctx.db.session() as session:
        rule = ApprovalRule(**fields, created_at=utcnow())
        session.add(rule)
        session.flush()
        return rule_dict(rule)


def rule_dict(rule: ApprovalRule) -> dict[str, Any]:
    return {
        "id": rule.id,
        "company_id": rule.company_id,
        "cost_center": rule.cost_center,
        "supplier_ruc": rule.supplier_ruc,
        "min_amount_cents": rule.min_amount_cents,
        "max_amount_cents": rule.max_amount_cents,
        "level": rule.level,
        "approver": rule.approver,
        "sla_hours": rule.sla_hours,
        "escalate_to": rule.escalate_to,
        "active": rule.active,
    }


def list_rules(ctx: AppContext) -> list[dict[str, Any]]:
    with ctx.db.session() as session:
        rules = session.scalars(
            select(ApprovalRule).order_by(ApprovalRule.level, ApprovalRule.min_amount_cents)
        )
        return [rule_dict(r) for r in rules]


def disable_rule(ctx: AppContext, rule_id: int) -> bool:
    with ctx.db.session() as session:
        rule = session.get(ApprovalRule, rule_id)
        if rule is None:
            return False
        rule.active = False
        return True


def delegate(
    ctx: AppContext, *, username: str, to: str, starts_on: date, ends_on: date, reason: str | None
) -> dict[str, Any]:
    if username == to:
        raise UserError("No se puede delegar en uno mismo")
    if ends_on < starts_on:
        raise UserError("La fecha final es anterior a la inicial")
    with ctx.db.session() as session:
        row = ApprovalDelegation(
            username=username,
            delegate=to,
            starts_on=starts_on,
            ends_on=ends_on,
            reason=reason,
            created_at=utcnow(),
        )
        session.add(row)
        session.flush()
        return {
            "id": row.id,
            "username": username,
            "delegate": to,
            "starts_on": starts_on,
            "ends_on": ends_on,
        }
