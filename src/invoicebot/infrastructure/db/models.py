"""Modelos ORM (SQLAlchemy 2.0) de las tablas de compras, aprobaciones e integración.

El esquema lo crean las migraciones T-SQL (``migrations_v1.py``): estos modelos solo
mapean tablas que ya existen. ``tests/integration/test_models.py`` comprueba que cada
columna mapeada exista en la base.

``Invoice`` mapea solo las columnas que usa el código ORM; el resto de la aplicación
sigue leyendo `invoices` con SQL. La tabla tiene un trigger de auditoría, por eso
``implicit_returning=False`` (SQL Server no admite OUTPUT sin INTO en ese caso).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    ForeignKey,
    Integer,
    LargeBinary,
    Numeric,
    Unicode,
    UnicodeText,
)
from sqlalchemy.dialects.mssql import DATETIME2
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

Stamp = DATETIME2(precision=3)


def utcnow() -> datetime:
    """DATETIME2 guarda UTC sin zona."""
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Company(Base):
    __tablename__ = "companies"
    # Trigger de auditoría (company_changes): sin OUTPUT.
    __table_args__ = {"implicit_returning": False}  # noqa: RUF012 (convención de SQLAlchemy)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ruc: Mapped[str] = mapped_column(Unicode(11))
    name: Mapped[str] = mapped_column(Unicode(200))
    retention_agent: Mapped[bool] = mapped_column(Boolean, default=False)
    good_taxpayer: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(Stamp, default=utcnow)


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = {"implicit_returning": False}  # noqa: RUF012 (convención de SQLAlchemy)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(Integer)
    company_id: Mapped[int | None] = mapped_column(Integer)
    supplier_ruc: Mapped[str | None] = mapped_column(Unicode(11))
    supplier_name: Mapped[str | None] = mapped_column(Unicode(300))
    customer_ruc: Mapped[str | None] = mapped_column(Unicode(11))
    document_type: Mapped[str | None] = mapped_column(Unicode(2))
    series: Mapped[str | None] = mapped_column(Unicode(10))
    number: Mapped[str | None] = mapped_column(Unicode(20))
    issue_date: Mapped[date | None] = mapped_column(Date)
    due_date: Mapped[date | None] = mapped_column(Date)
    currency: Mapped[str | None] = mapped_column(Unicode(3))
    subtotal_cents: Mapped[int | None] = mapped_column(BigInteger)
    igv_cents: Mapped[int | None] = mapped_column(BigInteger)
    total_cents: Mapped[int | None] = mapped_column(BigInteger)
    exchange_rate: Mapped[Decimal | None] = mapped_column(Numeric(9, 4))
    validation_status: Mapped[str] = mapped_column(Unicode(10))
    review_status: Mapped[str] = mapped_column(Unicode(10))
    sunat_status: Mapped[str | None] = mapped_column(Unicode(30))
    # Migración 008
    approval_status: Mapped[str | None] = mapped_column(Unicode(12))
    account_code: Mapped[str | None] = mapped_column(Unicode(20))
    cost_center: Mapped[str | None] = mapped_column(Unicode(30))
    accounting_source: Mapped[str | None] = mapped_column(Unicode(12))
    applied_to_invoice_id: Mapped[int | None] = mapped_column(Integer)

    @property
    def label(self) -> str:
        return f"{self.series or '?'}-{self.number or '?'}"

    @property
    def net_cents(self) -> int:
        """Importe sin IGV (lo que se compara con la orden de compra)."""
        if self.subtotal_cents is not None:
            return int(self.subtotal_cents)
        return int(self.total_cents or 0) - int(self.igv_cents or 0)

    def amount_pen_cents(self) -> int:
        """Total en soles (con el tipo de cambio guardado), para la matriz de aprobación."""
        total = int(self.total_cents or 0)
        if (self.currency or "PEN") == "PEN" or self.exchange_rate is None:
            return total
        return round(total * float(self.exchange_rate))


# --- Compras ---------------------------------------------------------------------------


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("companies.id"))
    po_number: Mapped[str] = mapped_column(Unicode(40))
    supplier_ruc: Mapped[str] = mapped_column(Unicode(11))
    supplier_name: Mapped[str | None] = mapped_column(Unicode(300))
    currency: Mapped[str] = mapped_column(Unicode(3), default="PEN")
    issue_date: Mapped[date | None] = mapped_column(Date)
    #: Importe sin IGV.
    total_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    status: Mapped[str] = mapped_column(Unicode(20), default="open")
    requires_receipt: Mapped[bool] = mapped_column(Boolean, default=True)
    cost_center: Mapped[str | None] = mapped_column(Unicode(30))
    requester: Mapped[str | None] = mapped_column(Unicode(150))
    source: Mapped[str] = mapped_column(Unicode(20), default="api")
    created_at: Mapped[datetime] = mapped_column(Stamp, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(Stamp, default=utcnow)

    lines: Mapped[list[PurchaseOrderLine]] = relationship(
        back_populates="order", cascade="all, delete-orphan", order_by="PurchaseOrderLine.line_no"
    )


class PurchaseOrderLine(Base):
    __tablename__ = "purchase_order_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(Integer, ForeignKey("purchase_orders.id"))
    line_no: Mapped[int] = mapped_column(Integer)
    item_code: Mapped[str | None] = mapped_column(Unicode(60))
    description: Mapped[str | None] = mapped_column(Unicode(300))
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    unit_price_cents: Mapped[int] = mapped_column(BigInteger)

    order: Mapped[PurchaseOrder] = relationship(back_populates="lines")

    @property
    def amount_cents(self) -> int:
        return round(float(self.quantity) * self.unit_price_cents)


class GoodsReceipt(Base):
    """Nota de ingreso, guía de remisión o conformidad de servicio."""

    __tablename__ = "goods_receipts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("companies.id"))
    order_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("purchase_orders.id"))
    receipt_number: Mapped[str] = mapped_column(Unicode(40))
    supplier_ruc: Mapped[str] = mapped_column(Unicode(11))
    receipt_date: Mapped[date | None] = mapped_column(Date)
    #: Guía de remisión del proveedor (serie-número), si la hay.
    despatch_reference: Mapped[str | None] = mapped_column(Unicode(40))
    #: Valor recibido sin IGV cuando no hay líneas vinculadas a la orden.
    amount_cents: Mapped[int | None] = mapped_column(BigInteger)
    source: Mapped[str] = mapped_column(Unicode(20), default="api")
    created_at: Mapped[datetime] = mapped_column(Stamp, default=utcnow)

    lines: Mapped[list[GoodsReceiptLine]] = relationship(
        back_populates="receipt", cascade="all, delete-orphan"
    )
    order: Mapped[PurchaseOrder | None] = relationship()


class GoodsReceiptLine(Base):
    __tablename__ = "goods_receipt_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    receipt_id: Mapped[int] = mapped_column(Integer, ForeignKey("goods_receipts.id"))
    order_line_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("purchase_order_lines.id")
    )
    item_code: Mapped[str | None] = mapped_column(Unicode(60))
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4))

    receipt: Mapped[GoodsReceipt] = relationship(back_populates="lines")
    order_line: Mapped[PurchaseOrderLine | None] = relationship()


class InvoiceMatch(Base):
    __tablename__ = "invoice_matches"

    invoice_id: Mapped[int] = mapped_column(Integer, ForeignKey("invoices.id"), primary_key=True)
    order_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("purchase_orders.id"))
    #: matched | exception | no_order
    status: Mapped[str] = mapped_column(Unicode(12))
    #: two_way | three_way
    match_type: Mapped[str | None] = mapped_column(Unicode(10))
    #: Cómo se encontró la orden: xml_order | xml_despatch | supplier_amount | manual
    found_by: Mapped[str | None] = mapped_column(Unicode(20))
    matched_cents: Mapped[int | None] = mapped_column(BigInteger)
    exceptions: Mapped[str | None] = mapped_column(UnicodeText)
    checked_at: Mapped[datetime] = mapped_column(Stamp, default=utcnow)


# --- Aprobaciones ----------------------------------------------------------------------


class ApprovalRule(Base):
    __tablename__ = "approval_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("companies.id"))
    cost_center: Mapped[str | None] = mapped_column(Unicode(30))
    supplier_ruc: Mapped[str | None] = mapped_column(Unicode(11))
    min_amount_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    max_amount_cents: Mapped[int | None] = mapped_column(BigInteger)
    level: Mapped[int] = mapped_column(Integer, default=1)
    #: NULL: el nivel se aprueba solo (si la factura coincide con su orden de compra).
    approver: Mapped[str | None] = mapped_column(Unicode(40))
    sla_hours: Mapped[int] = mapped_column(Integer, default=48)
    escalate_to: Mapped[str | None] = mapped_column(Unicode(40))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(Stamp, default=utcnow)


class ApprovalRequest(Base):
    __tablename__ = "approval_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invoice_id: Mapped[int] = mapped_column(Integer, ForeignKey("invoices.id"))
    level: Mapped[int] = mapped_column(Integer)
    rule_id: Mapped[int | None] = mapped_column(Integer)
    approver: Mapped[str] = mapped_column(Unicode(40))
    #: pending | approved | rejected | escalated | cancelled
    status: Mapped[str] = mapped_column(Unicode(12), default="pending")
    due_at: Mapped[datetime] = mapped_column(Stamp)
    escalate_to: Mapped[str | None] = mapped_column(Unicode(40))
    reminders_sent: Mapped[int] = mapped_column(Integer, default=0)
    last_reminder_at: Mapped[datetime | None] = mapped_column(Stamp)
    decided_at: Mapped[datetime | None] = mapped_column(Stamp)
    decided_by: Mapped[str | None] = mapped_column(Unicode(150))
    comment: Mapped[str | None] = mapped_column(Unicode(1000))
    #: SHA-256 del enlace de un clic enviado por correo (se borra al usarse).
    token_hash: Mapped[bytes | None] = mapped_column(LargeBinary(32))
    token_expires_at: Mapped[datetime | None] = mapped_column(Stamp)
    created_at: Mapped[datetime] = mapped_column(Stamp, default=utcnow)


class ApprovalDelegation(Base):
    __tablename__ = "approval_delegations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(Unicode(40))
    delegate: Mapped[str] = mapped_column(Unicode(40))
    starts_on: Mapped[date] = mapped_column(Date)
    ends_on: Mapped[date] = mapped_column(Date)
    reason: Mapped[str | None] = mapped_column(Unicode(300))
    created_at: Mapped[datetime] = mapped_column(Stamp, default=utcnow)


class SupplierAccounting(Base):
    """Cuenta contable y centro de costo aprendidos de las correcciones por proveedor."""

    __tablename__ = "supplier_accounting"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_key: Mapped[int] = mapped_column(Integer, default=0)
    supplier_ruc: Mapped[str] = mapped_column(Unicode(11))
    account_code: Mapped[str | None] = mapped_column(Unicode(20))
    cost_center: Mapped[str | None] = mapped_column(Unicode(30))
    uses: Mapped[int] = mapped_column(Integer, default=1)
    last_used_at: Mapped[datetime] = mapped_column(Stamp, default=utcnow)


# --- Integración: eventos y webhooks ------------------------------------------------------


class Webhook(Base):
    __tablename__ = "webhooks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Unicode(100))
    url: Mapped[str] = mapped_column(Unicode(500))
    #: Secreto para la firma HMAC-SHA256 (cabecera X-InvoiceBot-Signature).
    secret: Mapped[str] = mapped_column(Unicode(100))
    #: Tipos separados por comas; "*" = todos; "invoice.*" = un prefijo.
    events: Mapped[str] = mapped_column(Unicode(500), default="*")
    company_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("companies.id"))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(Stamp, default=utcnow)

    def wants(self, event_type: str, company_id: int | None) -> bool:
        if not self.active:
            return False
        if self.company_id is not None and company_id != self.company_id:
            return False
        for pattern in (p.strip() for p in self.events.split(",")):
            if pattern in ("*", event_type):
                return True
            if pattern.endswith(".*") and event_type.startswith(pattern[:-1]):
                return True
        return False


class OutboxEvent(Base):
    """Evento de negocio (CloudEvents) guardado en la misma transacción que el cambio."""

    __tablename__ = "outbox_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_id: Mapped[str] = mapped_column(Unicode(36))
    type: Mapped[str] = mapped_column(Unicode(80))
    subject: Mapped[str | None] = mapped_column(Unicode(100))
    company_id: Mapped[int | None] = mapped_column(Integer)
    data: Mapped[str] = mapped_column(UnicodeText)
    created_at: Mapped[datetime] = mapped_column(Stamp, default=utcnow)

    deliveries: Mapped[list[WebhookDelivery]] = relationship(back_populates="event")

    def envelope(self, source: str) -> dict[str, Any]:
        import json

        stamp = (
            self.created_at.isoformat(timespec="milliseconds") + "Z" if self.created_at else None
        )
        return {
            "specversion": "1.0",
            "id": self.event_id,
            "type": self.type,
            "source": source,
            "subject": self.subject,
            "time": stamp,
            "datacontenttype": "application/json",
            "data": json.loads(self.data),
        }


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("outbox_events.id"))
    webhook_id: Mapped[int] = mapped_column(Integer, ForeignKey("webhooks.id"))
    #: pending | delivered | failed
    status: Mapped[str] = mapped_column(Unicode(12), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_status_code: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(Unicode(1000))
    delivered_at: Mapped[datetime | None] = mapped_column(Stamp)
    created_at: Mapped[datetime] = mapped_column(Stamp, default=utcnow)

    event: Mapped[OutboxEvent] = relationship(back_populates="deliveries")
    webhook: Mapped[Webhook] = relationship()
