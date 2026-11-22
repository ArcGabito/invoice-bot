"""Empresas (RUC adquirentes) de una instalación multiempresa."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from invoicebot.infrastructure.db.connection import Database
from invoicebot.infrastructure.db.invoice_repository import Row, as_row

_COLUMNS = "id, ruc, name, retention_agent, good_taxpayer, active, created_at"


class CompanyRepository:
    def __init__(self, db: Database):
        self.db = db

    def _one(self, sql: str, params: dict[str, Any] | None = None) -> Row | None:
        with self.db.begin() as conn:
            row = conn.execute(text(sql), params or {}).first()
        return as_row(row._mapping) if row is not None else None

    def _all(self, sql: str, params: dict[str, Any] | None = None) -> list[Row]:
        with self.db.begin() as conn:
            return [as_row(r._mapping) for r in conn.execute(text(sql), params or {})]

    def add(
        self, *, ruc: str, name: str, retention_agent: bool = False, good_taxpayer: bool = False
    ) -> Row:
        # `companies` tiene un trigger de auditoría: sin OUTPUT (exige INTO).
        with self.db.begin() as conn:
            company_id = conn.execute(
                text(
                    """SET NOCOUNT ON;
                       INSERT INTO companies (ruc, name, retention_agent, good_taxpayer)
                       VALUES (:r, :n, :ra, :gt);
                       SELECT CAST(SCOPE_IDENTITY() AS INT)"""
                ),
                {"r": ruc, "n": name[:200], "ra": int(retention_agent), "gt": int(good_taxpayer)},
            ).scalar_one()
        row = self.get(int(company_id))
        assert row is not None
        return row

    def update(self, company_id: int, **fields: Any) -> None:
        allowed = {"name", "retention_agent", "good_taxpayer", "active"}
        sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not sets:
            return
        with self.db.begin() as conn:
            conn.execute(
                text(
                    f"UPDATE companies SET {', '.join(f'{k} = :{k}' for k in sets)} WHERE id = :id"
                ),
                {
                    **{k: (int(v) if isinstance(v, bool) else v) for k, v in sets.items()},
                    "id": company_id,
                },
            )

    def get(self, company_id: int) -> Row | None:
        return self._one(f"SELECT {_COLUMNS} FROM companies WHERE id = :id", {"id": company_id})

    def get_by_ruc(self, ruc: str) -> Row | None:
        return self._one(f"SELECT {_COLUMNS} FROM companies WHERE ruc = :r", {"r": ruc})

    def list_companies(
        self, *, ids: list[int] | None = None, active_only: bool = False
    ) -> list[Row]:
        rows = self._all(
            f"SELECT {_COLUMNS} FROM companies"
            + (" WHERE active = 1" if active_only else "")
            + " ORDER BY name"
        )
        if ids is not None:
            allowed = set(ids)
            rows = [r for r in rows if r["id"] in allowed]
        return rows

    def assign_invoices(self, company_id: int) -> int:
        """Asigna a la empresa los comprobantes ya guardados dirigidos a su RUC."""
        with self.db.begin() as conn:
            return int(
                conn.execute(
                    text(
                        """UPDATE i SET company_id = c.id FROM invoices i
                           JOIN companies c ON c.ruc = i.customer_ruc
                           WHERE c.id = :id AND i.company_id IS NULL"""
                    ),
                    {"id": company_id},
                ).rowcount
                or 0
            )

    def history(self, company_id: int) -> list[Row]:
        """Cada estado de la empresa (trigger de auditoría), con quién lo cambió."""
        return self._all(
            """SELECT operation, changed_at, changed_by, ruc, name, retention_agent,
                      good_taxpayer, active
               FROM company_changes WHERE company_id = :id ORDER BY id""",
            {"id": company_id},
        )
