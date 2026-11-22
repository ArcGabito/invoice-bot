"""Administración de webhooks y consulta de eventos."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select

from invoicebot.application.events import EVENT_TYPES, new_secret
from invoicebot.errors import UserError
from invoicebot.infrastructure.db.models import OutboxEvent, Webhook, WebhookDelivery, utcnow

if TYPE_CHECKING:
    from invoicebot.context import AppContext


def _check_patterns(events: str) -> str:
    patterns = [p.strip() for p in events.split(",") if p.strip()]
    if not patterns:
        raise UserError("Indica al menos un evento (o * para todos)")
    for pattern in patterns:
        if pattern == "*" or pattern in EVENT_TYPES:
            continue
        if pattern.endswith(".*") and any(t.startswith(pattern[:-1]) for t in EVENT_TYPES):
            continue
        raise UserError(f"Evento desconocido: {pattern} (ver GET /api/v1/webhooks)")
    return ",".join(patterns)


def add_webhook(
    ctx: AppContext, *, name: str, url: str, events: str = "*", company_ruc: str | None = None
) -> dict[str, Any]:
    if not url.startswith("https://") and not url.startswith("http://"):
        raise UserError("La URL del webhook debe ser http(s)")
    company_id = None
    if company_ruc:
        from invoicebot.application.companies import require_company

        company_id = int(require_company(ctx.companies, company_ruc)["id"])
    secret = new_secret()
    with ctx.db.session() as session:
        hook = Webhook(
            name=name.strip()[:100],
            url=url,
            secret=secret,
            events=_check_patterns(events),
            company_id=company_id,
            active=True,
            created_at=utcnow(),
        )
        session.add(hook)
        session.flush()
        return {
            "id": hook.id,
            "name": hook.name,
            "url": hook.url,
            "events": hook.events,
            "company_id": hook.company_id,
            "secret": secret,
        }


def list_webhooks(ctx: AppContext) -> list[dict[str, Any]]:
    with ctx.db.session() as session:
        stats = dict(
            session.execute(
                select(WebhookDelivery.webhook_id, func.count())
                .where(WebhookDelivery.status == "failed")
                .group_by(WebhookDelivery.webhook_id)
            ).all()
        )
        return [
            {
                "id": h.id,
                "name": h.name,
                "url": h.url,
                "events": h.events,
                "company_id": h.company_id,
                "active": h.active,
                "failed_deliveries": int(stats.get(h.id, 0)),
            }
            for h in session.scalars(select(Webhook).order_by(Webhook.id))
        ]


def disable_webhook(ctx: AppContext, webhook_id: int) -> bool:
    with ctx.db.session() as session:
        hook = session.get(Webhook, webhook_id)
        if hook is None:
            return False
        hook.active = False
        return True


def list_events(ctx: AppContext, *, event_type: str | None, limit: int) -> list[dict[str, Any]]:
    with ctx.db.session() as session:
        query = select(OutboxEvent).order_by(OutboxEvent.id.desc()).limit(limit)
        if event_type:
            query = query.where(OutboxEvent.type == event_type)
        return [
            {
                "id": e.event_id,
                "type": e.type,
                "subject": e.subject,
                "company_id": e.company_id,
                "time": e.created_at,
                "data": json.loads(e.data),
                "deliveries": [
                    {"webhook_id": d.webhook_id, "status": d.status, "attempts": d.attempts}
                    for d in e.deliveries
                ],
            }
            for e in session.scalars(query)
        ]
