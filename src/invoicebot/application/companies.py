"""Empresas de una instalación multiempresa (estudio contable o grupo empresarial)."""

from __future__ import annotations

from typing import Any

from invoicebot.domain.ruc import normalize_ruc, validate_ruc
from invoicebot.errors import UserError
from invoicebot.infrastructure.db.company_repository import CompanyRepository


def check_ruc(value: object) -> str:
    ruc = normalize_ruc(value) or ""
    result = validate_ruc(ruc)
    if not result.valid:
        raise UserError(f"RUC no válido: {value!r} ({result.reason})")
    return ruc


def add_company(
    companies: CompanyRepository,
    *,
    ruc: str,
    name: str,
    retention_agent: bool = False,
    good_taxpayer: bool = False,
) -> dict[str, Any]:
    """Registra la empresa y le asigna los comprobantes ya guardados dirigidos a su RUC."""
    ruc = check_ruc(ruc)
    if companies.get_by_ruc(ruc):
        raise UserError(f"La empresa con RUC {ruc} ya existe")
    if not name.strip():
        raise UserError("Indica la razón social")
    company = companies.add(
        ruc=ruc,
        name=name.strip(),
        retention_agent=retention_agent,
        good_taxpayer=good_taxpayer,
    )
    company["assigned_invoices"] = companies.assign_invoices(int(company["id"]))
    return company


def require_company(companies: CompanyRepository, ruc: str) -> dict[str, Any]:
    company = companies.get_by_ruc(check_ruc(ruc))
    if company is None:
        raise UserError(f"No existe la empresa con RUC {ruc}")
    return company
