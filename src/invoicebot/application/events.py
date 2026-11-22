"""Eventos de negocio y webhooks firmados (patrón outbox).

1. ``publish(session, ...)`` guarda el evento (formato CloudEvents) y una entrega por
   cada webhook suscrito **en la misma transacción** que el cambio de negocio: si la
   transacción falla, no hay evento; si confirma, el evento no se pierde.
2. ``dispatch(ctx)`` encola un trabajo ``webhooks.deliver`` por entrega pendiente. Lo
   llama quien publica tras confirmar y, por si acaso, el programador (``webhooks.sweep``).
3. ``deliver`` hace el POST con firma HMAC-SHA256 y reintenta con espera exponencial.

Cabeceras de cada entrega:
- ``X-InvoiceBot-Event``: tipo (p. ej. ``invoice.approved_for_payment``).
- ``X-InvoiceBot-Delivery``: id de la entrega (idempotencia en el receptor).
- ``X-InvoiceBot-Timestamp``: segundos Unix.
- ``X-InvoiceBot-Signature``: ``sha256=<hex>`` de ``HMAC(secreto, "<timestamp>.<cuerpo>")``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, true
from sqlalchemy.orm import Session

from invoicebot.infrastructure.db.models import OutboxEvent, Webhook, WebhookDelivery, utcnow

if TYPE_CHECKING:
    from invoicebot.context import AppContext

EVENT_SOURCE = "invoicebot"
#: Intentos de entrega antes de darla por fallida (con espera de 15 s a 1 h entre ellos).
MAX_DELIVERY_ATTEMPTS = 8

#: Catálogo de eventos (documentación y validación de suscripciones).
EVENT_TYPES = {
    "invoice.matched": "La factura coincide con su orden de compra (y recepción)",
    "invoice.match_exception": "La factura no coincide con su orden de compra",
    "invoice.note_applied": "Una nota de crédito o débito se aplicó a su factura",
    "approval.requested": "Hay una factura esperando aprobación (con enlace de un clic)",
    "approval.reminder": "Recordatorio de una aprobación vencida",
    "approval.escalated": "Una aprobación vencida pasó al siguiente responsable",
    "invoice.approved_for_payment": "La factura quedó autorizada para pago",
    "invoice.payment_rejected": "La factura fue rechazada en la aprobación",
}


def signature(secret: str, timestamp: str, body: bytes) -> str:
    digest = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return f"sha256={digest.hexdigest()}"


def new_secret() -> str:
    return f"whsec_{secrets.token_urlsafe(32)}"


def publish(
    session: Session,
    event_type: str,
    data: dict[str, Any],
    *,
    subject: str | None = None,
    company_id: int | None = None,
) -> OutboxEvent:
    """Guarda el evento y sus entregas en la transacción de ``session``."""
    if event_type not in EVENT_TYPES:
        raise ValueError(f"Evento desconocido: {event_type}")
    event = OutboxEvent(
        event_id=str(uuid.uuid4()),
        type=event_type,
        subject=subject,
        company_id=company_id,
        data=json.dumps(data, ensure_ascii=False, default=str),
        created_at=utcnow(),
    )
    session.add(event)
    hooks = session.scalars(select(Webhook).where(Webhook.active == true())).all()
    for hook in hooks:
        if hook.wants(event_type, company_id):
            event.deliveries.append(WebhookDelivery(webhook=hook, created_at=utcnow()))
    session.flush()
    return event


def dispatch(ctx: AppContext, *, limit: int = 500) -> int:
    """Encola una entrega por cada una pendiente (deduplicada mientras siga activa)."""
    with ctx.db.session() as session:
        ids = session.scalars(
            select(WebhookDelivery.id)
            .where(WebhookDelivery.status == "pending")
            .order_by(WebhookDelivery.id)
            .limit(limit)
        ).all()
    for delivery_id in ids:
        ctx.jobs.enqueue(
            "webhooks.deliver",
            {"delivery_id": int(delivery_id)},
            dedupe_key=f"webhooks.deliver:{delivery_id}",
        )
    return len(ids)


class DeliveryFailed(Exception):
    """El receptor no confirmó (se reintenta)."""


def deliver(ctx: AppContext, delivery_id: int) -> dict[str, Any]:
    with ctx.db.session() as session:
        delivery = session.get(WebhookDelivery, delivery_id)
        if delivery is None or delivery.status != "pending":
            return {"skipped": "la entrega no está pendiente"}
        hook, event = delivery.webhook, delivery.event
        body = json.dumps(event.envelope(EVENT_SOURCE), ensure_ascii=False).encode()
        url, secret, event_type = hook.url, hook.secret, event.type
    timestamp = str(int(time.time()))
    headers = {
        "content-type": "application/cloudevents+json; charset=utf-8",
        "user-agent": "InvoiceBot-Webhooks/1",
        "x-invoicebot-event": event_type,
        "x-invoicebot-delivery": str(delivery_id),
        "x-invoicebot-timestamp": timestamp,
        "x-invoicebot-signature": signature(secret, timestamp, body),
    }
    status_code: int | None = None
    error: str | None = None
    try:
        response = ctx.http.post(url, content=body, headers=headers, timeout=15)
        status_code = response.status_code
        if not 200 <= status_code < 300:
            error = f"HTTP {status_code}: {response.text[:300]}"
    except Exception as failure:  # red, DNS, TLS, tiempo de espera
        error = f"{type(failure).__name__}: {failure}"[:1000]
    with ctx.db.session() as session:
        row = session.get(WebhookDelivery, delivery_id)
        assert row is not None
        row.attempts += 1
        row.last_status_code = status_code
        row.last_error = error
        if error is None:
            row.status, row.delivered_at = "delivered", utcnow()
        elif row.attempts >= MAX_DELIVERY_ATTEMPTS:
            row.status = "failed"
        attempts, final = row.attempts, row.status
    if error is not None and final == "pending":
        raise DeliveryFailed(error)
    return {"status": final, "attempts": attempts, "http_status": status_code, "error": error}
