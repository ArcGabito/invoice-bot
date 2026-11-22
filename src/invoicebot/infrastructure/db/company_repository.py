"""Empresas (RUC adquirentes) de una instalación multiempresa (SQLAlchemy ORM)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import insert, select, update

from invoicebot.infrastructure.db.connection import Database
from invoicebot.infrastructure.db.invoice_repository import Row, as_row
from invoicebot.infrastructure.db.models import Company, CompanyChange, Invoice

_COLUMNS = (
    Company.id,
    Company.ruc,
    Company.name,
    Company.retention_agent,
    Company.good_taxpayer,
    Company.active,
    Company.created_at,
)


class CompanyRepository:
    def __init__(self, db: Database):
        self.db = db

    def add(
        self, *, ruc: str, name: str, retention_agent: bool = False, good_taxpayer: bool = False
    ) -> Row:
        # `companies` tiene un trigger de auditoría: el id llega por SCOPE_IDENTITY, sin OUTPUT.
        with self.db.begin() as conn:
            key = conn.execute(
                insert(Company).values(
                    ruc=ruc,
                    name=name[:200],
                    retention_agent=retention_agent,
                    good_taxpayer=good_taxpayer,
                )
            ).inserted_primary_key
        assert key is not None
        row = self.get(int(key[0]))
        assert row is not None
        return row

    def update(self, company_id: int, **fields: Any) -> None:
        allowed = {"name", "retention_agent", "good_taxpayer", "active"}
        values = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not values:
            return
        with self.db.begin() as conn:
            conn.execute(update(Company).where(Company.id == company_id).values(**values))

    def _one(self, *where: Any) -> Row | None:
        with self.db.connect() as conn:
            row = conn.execute(select(*_COLUMNS).where(*where)).mappings().first()
        return as_row(row) if row is not None else None

    def get(self, company_id: int) -> Row | None:
        return self._one(Company.id == company_id)

    def get_by_ruc(self, ruc: str) -> Row | None:
        return self._one(Company.ruc == ruc)

    def list_companies(
        self, *, ids: list[int] | None = None, active_only: bool = False
    ) -> list[Row]:
        query = select(*_COLUMNS).order_by(Company.name)
        if active_only:
            query = query.where(Company.active == True)  # noqa: E712 (SQL Server: = 1)
        if ids is not None:
            query = query.where(Company.id.in_(ids or [-1]))
        with self.db.connect() as conn:
            return [as_row(r) for r in conn.execute(query).mappings()]

    def assign_invoices(self, company_id: int) -> int:
        """Asigna a la empresa los comprobantes ya guardados dirigidos a su RUC."""
        with self.db.begin() as conn:
            ruc = conn.execute(select(Company.ruc).where(Company.id == company_id)).scalar_one()
            result = conn.execute(
                update(Invoice)
                .where(Invoice.customer_ruc == ruc, Invoice.company_id.is_(None))
                .values(company_id=company_id)
            )
            return int(result.rowcount or 0)

    def history(self, company_id: int) -> list[Row]:
        """Cada estado de la empresa (trigger de auditoría), con quién lo cambió."""
        query = (
            select(
                CompanyChange.operation,
                CompanyChange.changed_at,
                CompanyChange.changed_by,
                CompanyChange.ruc,
                CompanyChange.name,
                CompanyChange.retention_agent,
                CompanyChange.good_taxpayer,
                CompanyChange.active,
            )
            .where(CompanyChange.company_id == company_id)
            .order_by(CompanyChange.id)
        )
        with self.db.connect() as conn:
            return [as_row(r) for r in conn.execute(query).mappings()]
