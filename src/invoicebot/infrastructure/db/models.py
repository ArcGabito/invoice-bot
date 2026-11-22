"""Modelos ORM (SQLAlchemy 2.0) de todas las tablas de InvoiceBot.

Generados con ``sqlacodegen`` desde una base con todas las migraciones aplicadas y
ajustados a mano: clases en singular, solo las relaciones que usa el código, propiedades
de negocio y ``implicit_returning=False`` en las tablas con trigger de auditoría
(``invoices``, ``companies``: SQL Server no admite OUTPUT sin INTO en ellas).

El esquema lo siguen creando las migraciones T-SQL (``migrations_v*.py``): RLS,
triggers, índices filtrados y copia previa. Al agregar una migración, actualiza aquí el
modelo; ``tests/integration/test_purchasing.py::test_orm_models_match_the_schema``
comprueba que cada columna mapeada exista en la base.
"""

from __future__ import annotations

import datetime
import decimal
import json
from typing import Any

from sqlalchemy import (
    BINARY,
    DECIMAL,
    NCHAR,
    BigInteger,
    Boolean,
    Column,
    Computed,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    PrimaryKeyConstraint,
    Table,
    Unicode,
    UnicodeText,
    text,
)
from sqlalchemy.dialects.mssql import DATETIME2
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime.datetime:
    """DATETIME2 guarda UTC sin zona."""
    return datetime.datetime.now(datetime.UTC).replace(tzinfo=None)


class ApprovalDelegation(Base):
    __tablename__ = "approval_delegations"
    __table_args__ = (PrimaryKeyConstraint("id", name="pk_approval_delegations"),)

    id: Mapped[int] = mapped_column(
        Integer, Identity(start=1, increment=1), primary_key=True, autoincrement=True
    )
    username: Mapped[str] = mapped_column(Unicode(40), nullable=False)
    delegate: Mapped[str] = mapped_column(Unicode(40), nullable=False)
    starts_on: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    ends_on: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DATETIME2, nullable=False, server_default=text("(sysutcdatetime())")
    )
    reason: Mapped[str | None] = mapped_column(Unicode(300))


class BotRun(Base):
    __tablename__ = "bot_runs"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_bot_runs"),
        Index("ix_bot_runs_started", "started_at", mssql_clustered=False),
    )

    id: Mapped[int] = mapped_column(
        Integer, Identity(start=1, increment=1), primary_key=True, autoincrement=True
    )
    trigger: Mapped[str] = mapped_column(Unicode(10), nullable=False)
    status: Mapped[str] = mapped_column(Unicode(10), nullable=False)
    started_at: Mapped[datetime.datetime] = mapped_column(
        DATETIME2, nullable=False, server_default=text("(sysutcdatetime())")
    )
    exit_code: Mapped[int | None] = mapped_column(Integer)
    summary: Mapped[str | None] = mapped_column(UnicodeText())
    error_message: Mapped[str | None] = mapped_column(Unicode(4000))
    host: Mapped[str | None] = mapped_column(Unicode(255))
    pid: Mapped[int | None] = mapped_column(Integer)
    finished_at: Mapped[datetime.datetime | None] = mapped_column(DATETIME2)


class Company(Base):
    __tablename__ = "companies"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_companies"),
        Index("uq_companies_ruc", "ruc", mssql_clustered=False, unique=True),
        # Tiene un trigger de auditoría: SQL Server no admite OUTPUT sin INTO.
        {"implicit_returning": False},
    )

    id: Mapped[int] = mapped_column(
        Integer, Identity(start=1, increment=1), primary_key=True, autoincrement=True
    )
    ruc: Mapped[str] = mapped_column(Unicode(11), nullable=False)
    name: Mapped[str] = mapped_column(Unicode(200), nullable=False)
    retention_agent: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("((0))")
    )
    good_taxpayer: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("((0))")
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("((1))"))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DATETIME2, nullable=False, server_default=text("(sysutcdatetime())")
    )


class CompanyChange(Base):
    __tablename__ = "company_changes"
    __table_args__ = (PrimaryKeyConstraint("id", name="pk_company_changes"),)

    id: Mapped[int] = mapped_column(
        BigInteger, Identity(start=1, increment=1), primary_key=True, autoincrement=True
    )
    company_id: Mapped[int] = mapped_column(Integer, nullable=False)
    operation: Mapped[str] = mapped_column(NCHAR(1), nullable=False)
    changed_at: Mapped[datetime.datetime] = mapped_column(
        DATETIME2, nullable=False, server_default=text("(sysutcdatetime())")
    )
    changed_by: Mapped[str | None] = mapped_column(Unicode(150))
    ruc: Mapped[str | None] = mapped_column(Unicode(11))
    name: Mapped[str | None] = mapped_column(Unicode(200))
    retention_agent: Mapped[bool | None] = mapped_column(Boolean)
    good_taxpayer: Mapped[bool | None] = mapped_column(Boolean)
    active: Mapped[bool | None] = mapped_column(Boolean)


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        ForeignKeyConstraint(["linked_document_id"], ["documents.id"], name="fk_documents_linked"),
        PrimaryKeyConstraint("id", name="pk_documents"),
        Index("ix_documents_linked", "linked_document_id", mssql_clustered=False),
        Index("ix_documents_status", "status", mssql_clustered=False),
        Index("uq_documents_sha256", "sha256", mssql_clustered=False, unique=True),
    )

    id: Mapped[int] = mapped_column(
        Integer, Identity(start=1, increment=1), primary_key=True, autoincrement=True
    )
    sha256: Mapped[str] = mapped_column(Unicode(64), nullable=False)
    file_path: Mapped[str] = mapped_column(Unicode(1024), nullable=False)
    file_name: Mapped[str] = mapped_column(Unicode(260), nullable=False)
    mime_type: Mapped[str] = mapped_column(Unicode(100), nullable=False)
    file_size: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        Unicode(20), nullable=False, server_default=text("(N'pending')")
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("((0))"))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DATETIME2, nullable=False, server_default=text("(sysutcdatetime())")
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DATETIME2, nullable=False, server_default=text("(sysutcdatetime())")
    )
    text_method: Mapped[str | None] = mapped_column(Unicode(20))
    text_chars: Mapped[int | None] = mapped_column(Integer)
    page_count: Mapped[int | None] = mapped_column(Integer)
    error_kind: Mapped[str | None] = mapped_column(Unicode(50))
    error_message: Mapped[str | None] = mapped_column(Unicode(4000))
    skip_reason: Mapped[str | None] = mapped_column(Unicode(50))
    linked_document_id: Mapped[int | None] = mapped_column(Integer)
    processed_at: Mapped[datetime.datetime | None] = mapped_column(DATETIME2)


class ExchangeRate(Base):
    __tablename__ = "exchange_rates"
    __table_args__ = (PrimaryKeyConstraint("currency", "rate_date", name="pk_exchange_rates"),)

    currency: Mapped[str] = mapped_column(Unicode(3), primary_key=True)
    rate_date: Mapped[datetime.date] = mapped_column(Date, primary_key=True)
    sell: Mapped[decimal.Decimal] = mapped_column(DECIMAL(9, 4), nullable=False)
    source: Mapped[str] = mapped_column(Unicode(10), nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DATETIME2, nullable=False, server_default=text("(sysutcdatetime())")
    )
    buy: Mapped[decimal.Decimal | None] = mapped_column(DECIMAL(9, 4))


class InvoiceChange(Base):
    __tablename__ = "invoice_changes"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_invoice_changes"),
        Index("ix_invoice_changes_invoice", "invoice_id", "id", mssql_clustered=False),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, Identity(start=1, increment=1), primary_key=True, autoincrement=True
    )
    invoice_id: Mapped[int] = mapped_column(Integer, nullable=False)
    operation: Mapped[str] = mapped_column(NCHAR(1), nullable=False)
    changed_at: Mapped[datetime.datetime] = mapped_column(
        DATETIME2, nullable=False, server_default=text("(sysutcdatetime())")
    )
    changed_by: Mapped[str | None] = mapped_column(Unicode(150))
    company_id: Mapped[int | None] = mapped_column(Integer)
    supplier_ruc: Mapped[str | None] = mapped_column(Unicode(11))
    document_type: Mapped[str | None] = mapped_column(Unicode(2))
    series: Mapped[str | None] = mapped_column(Unicode(10))
    number: Mapped[str | None] = mapped_column(Unicode(20))
    issue_date: Mapped[datetime.date | None] = mapped_column(Date)
    currency: Mapped[str | None] = mapped_column(Unicode(3))
    igv_cents: Mapped[int | None] = mapped_column(BigInteger)
    total_cents: Mapped[int | None] = mapped_column(BigInteger)
    validation_status: Mapped[str | None] = mapped_column(Unicode(10))
    review_status: Mapped[str | None] = mapped_column(Unicode(10))
    reviewed_by: Mapped[str | None] = mapped_column(Unicode(100))
    sunat_status: Mapped[str | None] = mapped_column(Unicode(30))
    corrected: Mapped[bool | None] = mapped_column(Boolean)
    approval_status: Mapped[str | None] = mapped_column(Unicode(12))
    account_code: Mapped[str | None] = mapped_column(Unicode(20))
    cost_center: Mapped[str | None] = mapped_column(Unicode(30))


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_jobs"),
        Index(
            "ix_jobs_parent",
            "parent_id",
            mssql_clustered=False,
            mssql_where="([parent_id] IS NOT NULL)",
        ),
        Index(
            "ix_jobs_status",
            "status",
            "published_at",
            mssql_clustered=False,
            mssql_include=["run_after", "created_at"],
        ),
        Index("ix_jobs_task", "task", "id", mssql_clustered=False),
        Index(
            "uq_jobs_dedupe",
            "dedupe_key",
            mssql_clustered=False,
            mssql_where="([dedupe_key] IS NOT NULL)",
            unique=True,
        ),
        Index(
            "uq_jobs_idempotency",
            "idempotency_key",
            mssql_clustered=False,
            mssql_where="([idempotency_key] IS NOT NULL)",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, Identity(start=1, increment=1), primary_key=True, autoincrement=True
    )
    task: Mapped[str] = mapped_column(Unicode(100), nullable=False)
    queue: Mapped[str] = mapped_column(Unicode(40), nullable=False)
    status: Mapped[str] = mapped_column(Unicode(12), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("((0))"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DATETIME2, nullable=False, server_default=text("(sysutcdatetime())")
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DATETIME2, nullable=False, server_default=text("(sysutcdatetime())")
    )
    payload: Mapped[str | None] = mapped_column(UnicodeText())
    result: Mapped[str | None] = mapped_column(UnicodeText())
    error_message: Mapped[str | None] = mapped_column(Unicode(4000))
    dedupe_key: Mapped[str | None] = mapped_column(Unicode(200))
    idempotency_key: Mapped[str | None] = mapped_column(Unicode(200))
    parent_id: Mapped[int | None] = mapped_column(BigInteger)
    created_by: Mapped[str | None] = mapped_column(Unicode(150))
    worker: Mapped[str | None] = mapped_column(Unicode(200))
    run_after: Mapped[datetime.datetime | None] = mapped_column(DATETIME2)
    published_at: Mapped[datetime.datetime | None] = mapped_column(DATETIME2)
    started_at: Mapped[datetime.datetime | None] = mapped_column(DATETIME2)
    finished_at: Mapped[datetime.datetime | None] = mapped_column(DATETIME2)


class MailMessage(Base):
    __tablename__ = "mail_messages"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_mail_messages"),
        Index("uq_mail_messages", "account", "message_id", mssql_clustered=False, unique=True),
    )

    id: Mapped[int] = mapped_column(
        Integer, Identity(start=1, increment=1), primary_key=True, autoincrement=True
    )
    account: Mapped[str] = mapped_column(Unicode(320), nullable=False)
    message_id: Mapped[str] = mapped_column(Unicode(400), nullable=False)
    status: Mapped[str] = mapped_column(Unicode(20), nullable=False)
    files: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("((0))"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("((1))"))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DATETIME2, nullable=False, server_default=text("(sysutcdatetime())")
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DATETIME2, nullable=False, server_default=text("(sysutcdatetime())")
    )
    received_at: Mapped[datetime.datetime | None] = mapped_column(DATETIME2)
    sender: Mapped[str | None] = mapped_column(Unicode(320))
    subject: Mapped[str | None] = mapped_column(Unicode(500))
    folder: Mapped[str | None] = mapped_column(Unicode(400))
    error_message: Mapped[str | None] = mapped_column(UnicodeText())
    moved_to: Mapped[str | None] = mapped_column(Unicode(400))


class OidcRequest(Base):
    __tablename__ = "oidc_requests"
    __table_args__ = (PrimaryKeyConstraint("state", name="pk_oidc_requests"),)

    state: Mapped[str] = mapped_column(Unicode(64), primary_key=True)
    nonce: Mapped[str] = mapped_column(Unicode(64), nullable=False)
    code_verifier: Mapped[str] = mapped_column(Unicode(128), nullable=False)
    redirect_uri: Mapped[str] = mapped_column(Unicode(500), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DATETIME2, nullable=False, server_default=text("(sysutcdatetime())")
    )


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_outbox_events"),
        Index("ix_outbox_events_type", "type", "id", mssql_clustered=False),
        Index("uq_outbox_events_event", "event_id", mssql_clustered=False, unique=True),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, Identity(start=1, increment=1), primary_key=True, autoincrement=True
    )
    event_id: Mapped[str] = mapped_column(Unicode(36), nullable=False)
    type: Mapped[str] = mapped_column(Unicode(80), nullable=False)
    data: Mapped[str] = mapped_column(UnicodeText(), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DATETIME2, nullable=False, server_default=text("(sysutcdatetime())")
    )
    subject: Mapped[str | None] = mapped_column(Unicode(100))
    company_id: Mapped[int | None] = mapped_column(Integer)

    deliveries: Mapped[list[WebhookDelivery]] = relationship(back_populates="event")

    def envelope(self, source: str) -> dict[str, Any]:
        """El evento en formato CloudEvents 1.0."""
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


class Schedule(Base):
    __tablename__ = "schedules"
    __table_args__ = (PrimaryKeyConstraint("name", name="pk_schedules"),)

    name: Mapped[str] = mapped_column(Unicode(60), primary_key=True)
    task: Mapped[str] = mapped_column(Unicode(100), nullable=False)
    cron: Mapped[str] = mapped_column(Unicode(100), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("((1))"))
    source: Mapped[str] = mapped_column(
        Unicode(10), nullable=False, server_default=text("(N'config')")
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DATETIME2, nullable=False, server_default=text("(sysutcdatetime())")
    )
    payload: Mapped[str | None] = mapped_column(UnicodeText())
    next_run_at: Mapped[datetime.datetime | None] = mapped_column(DATETIME2)
    last_run_at: Mapped[datetime.datetime | None] = mapped_column(DATETIME2)
    last_job_id: Mapped[int | None] = mapped_column(BigInteger)


class SchemaMigration(Base):
    __tablename__ = "schema_migrations"
    __table_args__ = (PrimaryKeyConstraint("version", name="pk_schema_migrations"),)

    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Unicode(200), nullable=False)
    applied_at: Mapped[datetime.datetime] = mapped_column(
        DATETIME2, nullable=False, server_default=text("(sysutcdatetime())")
    )


class SupplierAccounting(Base):
    __tablename__ = "supplier_accounting"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_supplier_accounting"),
        Index("ix_supplier_accounting", "company_key", "supplier_ruc", mssql_clustered=False),
    )

    id: Mapped[int] = mapped_column(
        Integer, Identity(start=1, increment=1), primary_key=True, autoincrement=True
    )
    company_key: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("((0))"))
    supplier_ruc: Mapped[str] = mapped_column(Unicode(11), nullable=False)
    uses: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("((1))"))
    last_used_at: Mapped[datetime.datetime] = mapped_column(
        DATETIME2, nullable=False, server_default=text("(sysutcdatetime())")
    )
    account_code: Mapped[str | None] = mapped_column(Unicode(20))
    cost_center: Mapped[str | None] = mapped_column(Unicode(30))


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_users"),
        Index(
            "uq_users_email",
            "email",
            mssql_clustered=False,
            mssql_where="([email] IS NOT NULL)",
            unique=True,
        ),
        Index(
            "uq_users_subject",
            "external_subject",
            mssql_clustered=False,
            mssql_where="([external_subject] IS NOT NULL)",
            unique=True,
        ),
        Index("uq_users_username", "username", mssql_clustered=False, unique=True),
    )

    id: Mapped[int] = mapped_column(
        Integer, Identity(start=1, increment=1), primary_key=True, autoincrement=True
    )
    username: Mapped[str] = mapped_column(Unicode(40), nullable=False)
    display_name: Mapped[str] = mapped_column(Unicode(100), nullable=False)
    password_hash: Mapped[str] = mapped_column(Unicode(200), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("((1))"))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DATETIME2, nullable=False, server_default=text("(sysutcdatetime())")
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DATETIME2, nullable=False, server_default=text("(sysutcdatetime())")
    )
    role: Mapped[str] = mapped_column(
        Unicode(20), nullable=False, server_default=text("(N'admin')")
    )
    auth_provider: Mapped[str] = mapped_column(
        Unicode(20), nullable=False, server_default=text("(N'local')")
    )
    all_companies: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("((1))")
    )
    last_login_at: Mapped[datetime.datetime | None] = mapped_column(DATETIME2)
    email: Mapped[str | None] = mapped_column(Unicode(254))
    external_subject: Mapped[str | None] = mapped_column(Unicode(200))


V_INVOICE_BALANCES = Table(
    "v_invoice_balances",
    Base.metadata,
    Column("invoice_id", Integer, nullable=False),
    Column("company_id", Integer),
    Column("supplier_ruc", Unicode(11)),
    Column("series", Unicode(10)),
    Column("number", Unicode(20)),
    Column("currency", Unicode(3)),
    Column("total_cents", BigInteger),
    Column("credit_cents", BigInteger),
    Column("debit_cents", BigInteger),
    Column("balance_cents", BigInteger),
)


V_INVOICES = Table(
    "v_invoices",
    Base.metadata,
    Column("invoice_id", Integer, nullable=False),
    Column("document_id", Integer, nullable=False),
    Column("file_name", Unicode(260), nullable=False),
    Column("file_path", Unicode(1024), nullable=False),
    Column("supplier_name", Unicode(300)),
    Column("supplier_ruc", Unicode(11)),
    Column("supplier_tax_id", Unicode(50)),
    Column("supplier_country", Unicode(2)),
    Column("customer_name", Unicode(300)),
    Column("customer_ruc", Unicode(15)),
    Column("document_type", Unicode(2)),
    Column("series", Unicode(10)),
    Column("number", Unicode(20)),
    Column("issue_date", Date),
    Column("due_date", Date),
    Column("currency", Unicode(3)),
    Column("subtotal", DECIMAL(18, 2)),
    Column("taxable_amount", DECIMAL(18, 2)),
    Column("exempt_amount", DECIMAL(18, 2)),
    Column("unaffected_amount", DECIMAL(18, 2)),
    Column("discount_amount", DECIMAL(18, 2)),
    Column("igv", DECIMAL(18, 2)),
    Column("isc", DECIMAL(18, 2)),
    Column("icbper", DECIMAL(18, 2)),
    Column("other_charges", DECIMAL(18, 2)),
    Column("other_taxes", DECIMAL(18, 2)),
    Column("total", DECIMAL(18, 2)),
    Column("prepaid_amount", DECIMAL(18, 2)),
    Column("data_source", Unicode(10), nullable=False),
    Column("validation_status", Unicode(10), nullable=False),
    Column("review_status", Unicode(10), nullable=False),
    Column("reviewed_by", Unicode(100)),
    Column("reviewed_at", DATETIME2),
    Column("sunat_status", Unicode(30)),
    Column("sunat_checked_at", DATETIME2),
    Column("corrected", Boolean, nullable=False),
    Column("created_at", DATETIME2, nullable=False),
    Column("updated_at", DATETIME2, nullable=False),
    Column("detraction_code", Unicode(3)),
    Column("detraction_percent", DECIMAL(5, 2)),
    Column("detraction_amount", DECIMAL(18, 2)),
    Column("detraction_account", Unicode(30)),
    Column("exchange_rate", DECIMAL(9, 4)),
    Column("exchange_rate_date", Date),
    Column("total_pen", DECIMAL(18, 2)),
)


class ApiKey(Base):
    __tablename__ = "api_keys"
    __table_args__ = (
        ForeignKeyConstraint(["company_id"], ["companies.id"], name="fk_api_keys_company"),
        PrimaryKeyConstraint("id", name="pk_api_keys"),
        Index("uq_api_keys_hash", "key_hash", mssql_clustered=False, unique=True),
    )

    id: Mapped[int] = mapped_column(
        Integer, Identity(start=1, increment=1), primary_key=True, autoincrement=True
    )
    name: Mapped[str] = mapped_column(Unicode(100), nullable=False)
    prefix: Mapped[str] = mapped_column(Unicode(16), nullable=False)
    key_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    scope: Mapped[str] = mapped_column(Unicode(10), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("((1))"))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DATETIME2, nullable=False, server_default=text("(sysutcdatetime())")
    )
    created_by: Mapped[str | None] = mapped_column(Unicode(100))
    last_used_at: Mapped[datetime.datetime | None] = mapped_column(DATETIME2)
    revoked_at: Mapped[datetime.datetime | None] = mapped_column(DATETIME2)
    company_id: Mapped[int | None] = mapped_column(Integer)
    permissions: Mapped[str | None] = mapped_column(Unicode(400))


class ApprovalRule(Base):
    __tablename__ = "approval_rules"
    __table_args__ = (
        ForeignKeyConstraint(["company_id"], ["companies.id"], name="fk_approval_rules_company"),
        PrimaryKeyConstraint("id", name="pk_approval_rules"),
    )

    id: Mapped[int] = mapped_column(
        Integer, Identity(start=1, increment=1), primary_key=True, autoincrement=True
    )
    min_amount_cents: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("((0))")
    )
    level: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("((1))"))
    sla_hours: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("((48))"))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("((1))"))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DATETIME2, nullable=False, server_default=text("(sysutcdatetime())")
    )
    company_id: Mapped[int | None] = mapped_column(Integer)
    cost_center: Mapped[str | None] = mapped_column(Unicode(30))
    supplier_ruc: Mapped[str | None] = mapped_column(Unicode(11))
    max_amount_cents: Mapped[int | None] = mapped_column(BigInteger)
    approver: Mapped[str | None] = mapped_column(Unicode(40))
    escalate_to: Mapped[str | None] = mapped_column(Unicode(40))


class ExtractionRun(Base):
    __tablename__ = "extraction_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            ondelete="CASCADE",
            name="fk_extraction_runs_document",
        ),
        PrimaryKeyConstraint("id", name="pk_extraction_runs"),
        Index("ix_extraction_runs_document", "document_id", mssql_clustered=False),
    )

    id: Mapped[int] = mapped_column(
        Integer, Identity(start=1, increment=1), primary_key=True, autoincrement=True
    )
    document_id: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(Unicode(50), nullable=False)
    prompt_version: Mapped[str] = mapped_column(Unicode(100), nullable=False)
    status: Mapped[str] = mapped_column(Unicode(10), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DATETIME2, nullable=False, server_default=text("(sysutcdatetime())")
    )
    model: Mapped[str | None] = mapped_column(Unicode(100))
    text_method: Mapped[str | None] = mapped_column(Unicode(20))
    input_chars: Mapped[int | None] = mapped_column(Integer)
    error_kind: Mapped[str | None] = mapped_column(Unicode(50))
    error_message: Mapped[str | None] = mapped_column(Unicode(4000))
    raw_response: Mapped[str | None] = mapped_column(UnicodeText())
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)


class FileOccurrence(Base):
    __tablename__ = "file_occurrences"
    __table_args__ = (
        ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            ondelete="CASCADE",
            name="fk_file_occurrences_document",
        ),
        PrimaryKeyConstraint("id", name="pk_file_occurrences"),
        Index("ix_file_occurrences_document", "document_id", mssql_clustered=False),
        Index("ux_file_occurrences_path", "path_hash", mssql_clustered=False, unique=True),
    )

    id: Mapped[int] = mapped_column(
        Integer, Identity(start=1, increment=1), primary_key=True, autoincrement=True
    )
    document_id: Mapped[int] = mapped_column(Integer, nullable=False)
    file_path: Mapped[str] = mapped_column(Unicode(1024), nullable=False)
    first_seen_at: Mapped[datetime.datetime] = mapped_column(
        DATETIME2, nullable=False, server_default=text("(sysutcdatetime())")
    )
    last_seen_at: Mapped[datetime.datetime] = mapped_column(
        DATETIME2, nullable=False, server_default=text("(sysutcdatetime())")
    )
    path_hash: Mapped[bytes | None] = mapped_column(
        BINARY(32),
        Computed("(CONVERT([binary](32),hashbytes('SHA2_256',[file_path])))", persisted=True),
    )


class MailAttachment(Base):
    __tablename__ = "mail_attachments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["mail_message_id"],
            ["mail_messages.id"],
            ondelete="CASCADE",
            name="fk_mail_attachments_message",
        ),
        PrimaryKeyConstraint("mail_message_id", "sha256", name="pk_mail_attachments"),
        Index("ix_mail_attachments_sha256", "sha256", mssql_clustered=False),
    )

    mail_message_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sha256: Mapped[str] = mapped_column(Unicode(64), primary_key=True)
    file_name: Mapped[str] = mapped_column(Unicode(260), nullable=False)


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"
    __table_args__ = (
        ForeignKeyConstraint(["company_id"], ["companies.id"], name="fk_purchase_orders_company"),
        PrimaryKeyConstraint("id", name="pk_purchase_orders"),
        Index("ix_purchase_orders_supplier", "supplier_ruc", "status", mssql_clustered=False),
        Index(
            "uq_purchase_orders_number",
            "company_key",
            "po_number",
            mssql_clustered=False,
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer, Identity(start=1, increment=1), primary_key=True, autoincrement=True
    )
    company_key: Mapped[int] = mapped_column(
        Integer, Computed("(isnull([company_id],(0)))", persisted=True), nullable=False
    )
    po_number: Mapped[str] = mapped_column(Unicode(40), nullable=False)
    supplier_ruc: Mapped[str] = mapped_column(Unicode(11), nullable=False)
    currency: Mapped[str] = mapped_column(
        Unicode(3), nullable=False, server_default=text("(N'PEN')")
    )
    total_cents: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("((0))")
    )
    status: Mapped[str] = mapped_column(
        Unicode(20), nullable=False, server_default=text("(N'open')")
    )
    requires_receipt: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("((1))")
    )
    source: Mapped[str] = mapped_column(
        Unicode(20), nullable=False, server_default=text("(N'api')")
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DATETIME2, nullable=False, server_default=text("(sysutcdatetime())")
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DATETIME2, nullable=False, server_default=text("(sysutcdatetime())")
    )
    company_id: Mapped[int | None] = mapped_column(Integer)
    supplier_name: Mapped[str | None] = mapped_column(Unicode(300))
    issue_date: Mapped[datetime.date | None] = mapped_column(Date)
    cost_center: Mapped[str | None] = mapped_column(Unicode(30))
    requester: Mapped[str | None] = mapped_column(Unicode(150))

    lines: Mapped[list[PurchaseOrderLine]] = relationship(
        back_populates="order", cascade="all, delete-orphan", order_by="PurchaseOrderLine.line_no"
    )


class ReviewSession(Base):
    __tablename__ = "review_sessions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="CASCADE", name="fk_review_sessions_user"
        ),
        PrimaryKeyConstraint("token_hash", name="pk_review_sessions"),
        Index("ix_review_sessions_expires", "expires_at", mssql_clustered=False),
    )

    token_hash: Mapped[bytes] = mapped_column(BINARY(32), primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    user_version: Mapped[datetime.datetime] = mapped_column(DATETIME2, nullable=False)
    csrf_token: Mapped[str] = mapped_column(Unicode(64), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DATETIME2, nullable=False, server_default=text("(sysutcdatetime())")
    )
    expires_at: Mapped[datetime.datetime] = mapped_column(DATETIME2, nullable=False)
    ip: Mapped[str | None] = mapped_column(Unicode(64))


USER_COMPANIES = Table(
    "user_companies",
    Base.metadata,
    Column("user_id", Integer, primary_key=True),
    Column("company_id", Integer, primary_key=True),
    ForeignKeyConstraint(["company_id"], ["companies.id"], name="fk_user_companies_company"),
    ForeignKeyConstraint(
        ["user_id"], ["users.id"], ondelete="CASCADE", name="fk_user_companies_user"
    ),
    PrimaryKeyConstraint("user_id", "company_id", name="pk_user_companies"),
)


class Webhook(Base):
    __tablename__ = "webhooks"
    __table_args__ = (
        ForeignKeyConstraint(["company_id"], ["companies.id"], name="fk_webhooks_company"),
        PrimaryKeyConstraint("id", name="pk_webhooks"),
    )

    id: Mapped[int] = mapped_column(
        Integer, Identity(start=1, increment=1), primary_key=True, autoincrement=True
    )
    name: Mapped[str] = mapped_column(Unicode(100), nullable=False)
    url: Mapped[str] = mapped_column(Unicode(500), nullable=False)
    secret: Mapped[str] = mapped_column(Unicode(100), nullable=False)
    events: Mapped[str] = mapped_column(Unicode(500), nullable=False, server_default=text("(N'*')"))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("((1))"))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DATETIME2, nullable=False, server_default=text("(sysutcdatetime())")
    )
    company_id: Mapped[int | None] = mapped_column(Integer)

    def wants(self, event_type: str, company_id: int | None) -> bool:
        """¿Está suscrito a este evento? Patrones: "*", el tipo exacto o "invoice.*"."""
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


class GoodsReceipt(Base):
    __tablename__ = "goods_receipts"
    __table_args__ = (
        ForeignKeyConstraint(["company_id"], ["companies.id"], name="fk_goods_receipts_company"),
        ForeignKeyConstraint(["order_id"], ["purchase_orders.id"], name="fk_goods_receipts_order"),
        PrimaryKeyConstraint("id", name="pk_goods_receipts"),
        Index(
            "ix_goods_receipts_despatch",
            "supplier_ruc",
            "despatch_reference",
            mssql_clustered=False,
            mssql_where="([despatch_reference] IS NOT NULL)",
        ),
        Index("ix_goods_receipts_order", "order_id", mssql_clustered=False),
        Index(
            "uq_goods_receipts",
            "company_key",
            "supplier_ruc",
            "receipt_number",
            mssql_clustered=False,
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer, Identity(start=1, increment=1), primary_key=True, autoincrement=True
    )
    company_key: Mapped[int] = mapped_column(
        Integer, Computed("(isnull([company_id],(0)))", persisted=True), nullable=False
    )
    receipt_number: Mapped[str] = mapped_column(Unicode(40), nullable=False)
    supplier_ruc: Mapped[str] = mapped_column(Unicode(11), nullable=False)
    source: Mapped[str] = mapped_column(
        Unicode(20), nullable=False, server_default=text("(N'api')")
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DATETIME2, nullable=False, server_default=text("(sysutcdatetime())")
    )
    company_id: Mapped[int | None] = mapped_column(Integer)
    order_id: Mapped[int | None] = mapped_column(Integer)
    receipt_date: Mapped[datetime.date | None] = mapped_column(Date)
    despatch_reference: Mapped[str | None] = mapped_column(Unicode(40))
    amount_cents: Mapped[int | None] = mapped_column(BigInteger)

    lines: Mapped[list[GoodsReceiptLine]] = relationship(
        back_populates="receipt", cascade="all, delete-orphan"
    )
    order: Mapped[PurchaseOrder | None] = relationship()


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (
        ForeignKeyConstraint(["company_id"], ["companies.id"], name="fk_invoices_company"),
        ForeignKeyConstraint(
            ["document_id"], ["documents.id"], ondelete="CASCADE", name="fk_invoices_document"
        ),
        ForeignKeyConstraint(
            ["extraction_run_id"], ["extraction_runs.id"], name="fk_invoices_extraction_run"
        ),
        PrimaryKeyConstraint("id", name="pk_invoices"),
        Index(
            "ix_invoices_applied",
            "applied_to_invoice_id",
            mssql_clustered=False,
            mssql_where="([applied_to_invoice_id] IS NOT NULL)",
        ),
        Index(
            "ix_invoices_approval",
            "approval_status",
            mssql_clustered=False,
            mssql_where="([approval_status] IS NOT NULL)",
        ),
        Index("ix_invoices_business_key", "business_key", mssql_clustered=False),
        Index("ix_invoices_company", "company_id", "issue_date", mssql_clustered=False),
        Index(
            "ix_invoices_fx_pending",
            "currency",
            "issue_date",
            mssql_clustered=False,
            mssql_where="([exchange_rate] IS NULL AND [currency]<>N'PEN')",
        ),
        Index("ix_invoices_issue_date", "issue_date", mssql_clustered=False),
        Index("ix_invoices_review_status", "review_status", mssql_clustered=False),
        Index(
            "ix_invoices_supplier_doc",
            "supplier_ruc",
            "document_type",
            "series",
            "number",
            mssql_clustered=False,
        ),
        Index("uq_invoices_document", "document_id", mssql_clustered=False, unique=True),
        # Tiene un trigger de auditoría: SQL Server no admite OUTPUT sin INTO.
        {"implicit_returning": False},
    )

    id: Mapped[int] = mapped_column(
        Integer, Identity(start=1, increment=1), primary_key=True, autoincrement=True
    )
    document_id: Mapped[int] = mapped_column(Integer, nullable=False)
    extraction_run_id: Mapped[int] = mapped_column(Integer, nullable=False)
    data_source: Mapped[str] = mapped_column(
        Unicode(10), nullable=False, server_default=text("(N'ai')")
    )
    validation_status: Mapped[str] = mapped_column(Unicode(10), nullable=False)
    rules_version: Mapped[str] = mapped_column(Unicode(50), nullable=False)
    review_status: Mapped[str] = mapped_column(
        Unicode(10), nullable=False, server_default=text("(N'pending')")
    )
    corrected: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("((0))"))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DATETIME2, nullable=False, server_default=text("(sysutcdatetime())")
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DATETIME2, nullable=False, server_default=text("(sysutcdatetime())")
    )
    supplier_name: Mapped[str | None] = mapped_column(Unicode(300))
    supplier_ruc: Mapped[str | None] = mapped_column(Unicode(11))
    supplier_tax_id: Mapped[str | None] = mapped_column(Unicode(50))
    supplier_country: Mapped[str | None] = mapped_column(Unicode(2))
    customer_name: Mapped[str | None] = mapped_column(Unicode(300))
    customer_ruc: Mapped[str | None] = mapped_column(Unicode(15))
    document_type: Mapped[str | None] = mapped_column(Unicode(2))
    series: Mapped[str | None] = mapped_column(Unicode(10))
    number: Mapped[str | None] = mapped_column(Unicode(20))
    issue_date: Mapped[datetime.date | None] = mapped_column(Date)
    due_date: Mapped[datetime.date | None] = mapped_column(Date)
    currency: Mapped[str | None] = mapped_column(Unicode(3))
    subtotal_cents: Mapped[int | None] = mapped_column(BigInteger)
    taxable_amount_cents: Mapped[int | None] = mapped_column(BigInteger)
    exempt_amount_cents: Mapped[int | None] = mapped_column(BigInteger)
    unaffected_amount_cents: Mapped[int | None] = mapped_column(BigInteger)
    discount_amount_cents: Mapped[int | None] = mapped_column(BigInteger)
    igv_cents: Mapped[int | None] = mapped_column(BigInteger)
    isc_cents: Mapped[int | None] = mapped_column(BigInteger)
    icbper_cents: Mapped[int | None] = mapped_column(BigInteger)
    other_charges_cents: Mapped[int | None] = mapped_column(BigInteger)
    other_taxes_cents: Mapped[int | None] = mapped_column(BigInteger)
    total_cents: Mapped[int | None] = mapped_column(BigInteger)
    prepaid_amount_cents: Mapped[int | None] = mapped_column(BigInteger)
    business_key: Mapped[str | None] = mapped_column(Unicode(400))
    sunat_status: Mapped[str | None] = mapped_column(Unicode(30))
    sunat_checked_at: Mapped[datetime.datetime | None] = mapped_column(DATETIME2)
    reviewed_at: Mapped[datetime.datetime | None] = mapped_column(DATETIME2)
    reviewed_by: Mapped[str | None] = mapped_column(Unicode(100))
    detraction_code: Mapped[str | None] = mapped_column(Unicode(3))
    detraction_percent: Mapped[decimal.Decimal | None] = mapped_column(DECIMAL(5, 2))
    detraction_amount_cents: Mapped[int | None] = mapped_column(BigInteger)
    detraction_account: Mapped[str | None] = mapped_column(Unicode(30))
    exchange_rate: Mapped[decimal.Decimal | None] = mapped_column(DECIMAL(9, 4))
    exchange_rate_date: Mapped[datetime.date | None] = mapped_column(Date)
    company_id: Mapped[int | None] = mapped_column(Integer)
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


class PurchaseOrderLine(Base):
    __tablename__ = "purchase_order_lines"
    __table_args__ = (
        ForeignKeyConstraint(
            ["order_id"], ["purchase_orders.id"], ondelete="CASCADE", name="fk_po_lines_order"
        ),
        PrimaryKeyConstraint("id", name="pk_purchase_order_lines"),
        Index("uq_po_lines", "order_id", "line_no", mssql_clustered=False, unique=True),
    )

    id: Mapped[int] = mapped_column(
        Integer, Identity(start=1, increment=1), primary_key=True, autoincrement=True
    )
    order_id: Mapped[int] = mapped_column(Integer, nullable=False)
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity: Mapped[decimal.Decimal] = mapped_column(DECIMAL(18, 4), nullable=False)
    unit_price_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    item_code: Mapped[str | None] = mapped_column(Unicode(60))
    description: Mapped[str | None] = mapped_column(Unicode(300))

    order: Mapped[PurchaseOrder] = relationship(back_populates="lines")

    @property
    def amount_cents(self) -> int:
        return round(float(self.quantity) * self.unit_price_cents)


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        ForeignKeyConstraint(
            ["event_id"], ["outbox_events.id"], ondelete="CASCADE", name="fk_deliveries_event"
        ),
        ForeignKeyConstraint(
            ["webhook_id"], ["webhooks.id"], ondelete="CASCADE", name="fk_deliveries_webhook"
        ),
        PrimaryKeyConstraint("id", name="pk_webhook_deliveries"),
        Index(
            "ix_webhook_deliveries_pending",
            "status",
            "id",
            mssql_clustered=False,
            mssql_where="([status]=N'pending')",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, Identity(start=1, increment=1), primary_key=True, autoincrement=True
    )
    event_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    webhook_id: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        Unicode(12), nullable=False, server_default=text("(N'pending')")
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("((0))"))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DATETIME2, nullable=False, server_default=text("(sysutcdatetime())")
    )
    last_status_code: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(Unicode(1000))
    delivered_at: Mapped[datetime.datetime | None] = mapped_column(DATETIME2)

    event: Mapped[OutboxEvent] = relationship(back_populates="deliveries")
    webhook: Mapped[Webhook] = relationship()


class ApprovalRequest(Base):
    __tablename__ = "approval_requests"
    __table_args__ = (
        ForeignKeyConstraint(
            ["invoice_id"], ["invoices.id"], ondelete="CASCADE", name="fk_approval_requests_invoice"
        ),
        PrimaryKeyConstraint("id", name="pk_approval_requests"),
        Index("ix_approval_requests_invoice", "invoice_id", "level", mssql_clustered=False),
        Index(
            "ix_approval_requests_pending",
            "status",
            "due_at",
            mssql_clustered=False,
            mssql_include=["approver"],
        ),
        Index(
            "uq_approval_requests_token",
            "token_hash",
            mssql_clustered=False,
            mssql_where="([token_hash] IS NOT NULL)",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer, Identity(start=1, increment=1), primary_key=True, autoincrement=True
    )
    invoice_id: Mapped[int] = mapped_column(Integer, nullable=False)
    level: Mapped[int] = mapped_column(Integer, nullable=False)
    approver: Mapped[str] = mapped_column(Unicode(40), nullable=False)
    status: Mapped[str] = mapped_column(
        Unicode(12), nullable=False, server_default=text("(N'pending')")
    )
    due_at: Mapped[datetime.datetime] = mapped_column(DATETIME2, nullable=False)
    reminders_sent: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("((0))")
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DATETIME2, nullable=False, server_default=text("(sysutcdatetime())")
    )
    rule_id: Mapped[int | None] = mapped_column(Integer)
    escalate_to: Mapped[str | None] = mapped_column(Unicode(40))
    last_reminder_at: Mapped[datetime.datetime | None] = mapped_column(DATETIME2)
    decided_at: Mapped[datetime.datetime | None] = mapped_column(DATETIME2)
    decided_by: Mapped[str | None] = mapped_column(Unicode(150))
    comment: Mapped[str | None] = mapped_column(Unicode(1000))
    token_hash: Mapped[bytes | None] = mapped_column(BINARY(32))
    token_expires_at: Mapped[datetime.datetime | None] = mapped_column(DATETIME2)


class GoodsReceiptLine(Base):
    __tablename__ = "goods_receipt_lines"
    __table_args__ = (
        ForeignKeyConstraint(
            ["order_line_id"], ["purchase_order_lines.id"], name="fk_gr_lines_po_line"
        ),
        ForeignKeyConstraint(
            ["receipt_id"], ["goods_receipts.id"], ondelete="CASCADE", name="fk_gr_lines_receipt"
        ),
        PrimaryKeyConstraint("id", name="pk_goods_receipt_lines"),
    )

    id: Mapped[int] = mapped_column(
        Integer, Identity(start=1, increment=1), primary_key=True, autoincrement=True
    )
    receipt_id: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity: Mapped[decimal.Decimal] = mapped_column(DECIMAL(18, 4), nullable=False)
    order_line_id: Mapped[int | None] = mapped_column(Integer)
    item_code: Mapped[str | None] = mapped_column(Unicode(60))

    receipt: Mapped[GoodsReceipt] = relationship(back_populates="lines")
    order_line: Mapped[PurchaseOrderLine | None] = relationship()


class InvoiceMatch(Base):
    __tablename__ = "invoice_matches"
    __table_args__ = (
        ForeignKeyConstraint(
            ["invoice_id"], ["invoices.id"], ondelete="CASCADE", name="fk_invoice_matches_invoice"
        ),
        ForeignKeyConstraint(["order_id"], ["purchase_orders.id"], name="fk_invoice_matches_order"),
        PrimaryKeyConstraint("invoice_id", name="pk_invoice_matches"),
        Index(
            "ix_invoice_matches_order",
            "order_id",
            mssql_clustered=False,
            mssql_where="([order_id] IS NOT NULL)",
        ),
    )

    invoice_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(Unicode(12), nullable=False)
    checked_at: Mapped[datetime.datetime] = mapped_column(
        DATETIME2, nullable=False, server_default=text("(sysutcdatetime())")
    )
    order_id: Mapped[int | None] = mapped_column(Integer)
    match_type: Mapped[str | None] = mapped_column(Unicode(10))
    found_by: Mapped[str | None] = mapped_column(Unicode(20))
    matched_cents: Mapped[int | None] = mapped_column(BigInteger)
    exceptions: Mapped[str | None] = mapped_column(UnicodeText())


class ReviewAction(Base):
    __tablename__ = "review_actions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["invoice_id"], ["invoices.id"], ondelete="CASCADE", name="fk_review_actions_invoice"
        ),
        PrimaryKeyConstraint("id", name="pk_review_actions"),
        Index("ix_review_actions_invoice", "invoice_id", mssql_clustered=False),
    )

    id: Mapped[int] = mapped_column(
        Integer, Identity(start=1, increment=1), primary_key=True, autoincrement=True
    )
    invoice_id: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(Unicode(20), nullable=False)
    user: Mapped[str] = mapped_column(Unicode(100), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DATETIME2, nullable=False, server_default=text("(sysutcdatetime())")
    )
    field: Mapped[str | None] = mapped_column(Unicode(50))
    old_value: Mapped[str | None] = mapped_column(Unicode(1000))
    new_value: Mapped[str | None] = mapped_column(Unicode(1000))
    issue_id: Mapped[int | None] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(Unicode(1000))


class SunatCheck(Base):
    __tablename__ = "sunat_checks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["invoice_id"], ["invoices.id"], ondelete="CASCADE", name="fk_sunat_checks_invoice"
        ),
        PrimaryKeyConstraint("id", name="pk_sunat_checks"),
        Index("ix_sunat_checks_invoice", "invoice_id", mssql_clustered=False),
    )

    id: Mapped[int] = mapped_column(
        Integer, Identity(start=1, increment=1), primary_key=True, autoincrement=True
    )
    invoice_id: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Unicode(10), nullable=False)
    checked_at: Mapped[datetime.datetime] = mapped_column(
        DATETIME2, nullable=False, server_default=text("(sysutcdatetime())")
    )
    estado_cp: Mapped[str | None] = mapped_column(Unicode(5))
    estado_ruc: Mapped[str | None] = mapped_column(Unicode(5))
    cond_domi_ruc: Mapped[str | None] = mapped_column(Unicode(5))
    observations: Mapped[str | None] = mapped_column(UnicodeText())
    error_kind: Mapped[str | None] = mapped_column(Unicode(50))
    error_message: Mapped[str | None] = mapped_column(Unicode(4000))
    raw_response: Mapped[str | None] = mapped_column(UnicodeText())


class ValidationIssue(Base):
    __tablename__ = "validation_issues"
    __table_args__ = (
        ForeignKeyConstraint(
            ["invoice_id"], ["invoices.id"], ondelete="CASCADE", name="fk_validation_issues_invoice"
        ),
        PrimaryKeyConstraint("id", name="pk_validation_issues"),
        Index("ix_validation_issues_invoice", "invoice_id", mssql_clustered=False),
    )

    id: Mapped[int] = mapped_column(
        Integer, Identity(start=1, increment=1), primary_key=True, autoincrement=True
    )
    invoice_id: Mapped[int] = mapped_column(Integer, nullable=False)
    rule_code: Mapped[str] = mapped_column(Unicode(50), nullable=False)
    severity: Mapped[str] = mapped_column(Unicode(10), nullable=False)
    message: Mapped[str] = mapped_column(Unicode(1000), nullable=False)
    expected_value: Mapped[str | None] = mapped_column(Unicode(1000))
    actual_value: Mapped[str | None] = mapped_column(Unicode(1000))
    resolved_at: Mapped[datetime.datetime | None] = mapped_column(DATETIME2)
    resolved_by: Mapped[str | None] = mapped_column(Unicode(100))
    resolution_note: Mapped[str | None] = mapped_column(Unicode(1000))


# --- Migración 009: cumplimiento tributario --------------------------------------------


class SireProposal(Base):
    __tablename__ = "sire_proposals"

    id: Mapped[int] = mapped_column(Integer, Identity(start=1, increment=1), primary_key=True)
    company_id: Mapped[int | None] = mapped_column(Integer)
    period: Mapped[str] = mapped_column(NCHAR(6), nullable=False)
    source: Mapped[str] = mapped_column(Unicode(10), nullable=False, default="file")
    file_name: Mapped[str | None] = mapped_column(Unicode(260))
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by: Mapped[str | None] = mapped_column(Unicode(150))
    created_at: Mapped[datetime.datetime] = mapped_column(DATETIME2, nullable=False, default=utcnow)

    entries: Mapped[list[SireEntry]] = relationship(
        back_populates="proposal", cascade="all, delete-orphan"
    )


class SireEntry(Base):
    __tablename__ = "sire_entries"

    id: Mapped[int] = mapped_column(BigInteger, Identity(start=1, increment=1), primary_key=True)
    proposal_id: Mapped[int] = mapped_column(Integer, ForeignKey("sire_proposals.id"))
    supplier_ruc: Mapped[str | None] = mapped_column(Unicode(15))
    document_type: Mapped[str | None] = mapped_column(Unicode(2))
    series: Mapped[str | None] = mapped_column(Unicode(20))
    number: Mapped[str | None] = mapped_column(Unicode(20))
    issue_date: Mapped[datetime.date | None] = mapped_column(Date)
    currency: Mapped[str | None] = mapped_column(Unicode(3))
    total_cents: Mapped[int | None] = mapped_column(BigInteger)
    igv_cents: Mapped[int | None] = mapped_column(BigInteger)
    car: Mapped[str | None] = mapped_column(Unicode(40))
    raw: Mapped[str | None] = mapped_column(UnicodeText())

    proposal: Mapped[SireProposal] = relationship(back_populates="entries")


class DetractionDeposit(Base):
    __tablename__ = "detraction_deposits"

    id: Mapped[int] = mapped_column(Integer, Identity(start=1, increment=1), primary_key=True)
    invoice_id: Mapped[int] = mapped_column(Integer, ForeignKey("invoices.id"))
    constancia: Mapped[str] = mapped_column(Unicode(20), nullable=False)
    deposit_date: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source: Mapped[str] = mapped_column(Unicode(20), nullable=False, default="manual")
    created_by: Mapped[str | None] = mapped_column(Unicode(150))
    created_at: Mapped[datetime.datetime] = mapped_column(DATETIME2, nullable=False, default=utcnow)


class SupplierTaxStatus(Base):
    """Condición tributaria del proveedor (padrones de SUNAT o carga manual)."""

    __tablename__ = "supplier_tax_status"

    ruc: Mapped[str] = mapped_column(Unicode(11), primary_key=True)
    good_taxpayer: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    retention_agent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    perception_agent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source: Mapped[str] = mapped_column(Unicode(20), nullable=False, default="manual")
    updated_at: Mapped[datetime.datetime] = mapped_column(DATETIME2, nullable=False, default=utcnow)
