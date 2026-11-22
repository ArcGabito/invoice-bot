"""Exportaciones (CSV / Excel) y listados de duplicados."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from invoicebot.application.deps import Deps
from invoicebot.domain.invoice import ValidationStatus
from invoicebot.errors import ConfigError
from invoicebot.infrastructure.exports import render_csv, render_xlsx, write_export

_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
EXPORT_FORMATS = ("csv", "xlsx")


@dataclass
class ExportResult:
    path: str | None
    rows: int
    content: bytes
    file_name: str
    media_type: str


def check_month(month: str | None) -> str | None:
    if month is not None and not _MONTH.match(month):
        raise ConfigError(f'El periodo debe tener el formato AAAA-MM (recibido: "{month}")')
    return month


def build_export(
    deps: Deps,
    *,
    month: str | None = None,
    statuses: Sequence[str] | None = None,
    include_errors: bool = False,
    include_duplicates: bool = False,
    supplier_ruc: str | None = None,
    currency: str | None = None,
    fmt: str = "csv",
    delimiter: str = ",",
    bom: bool = False,
) -> ExportResult:
    """Por defecto: facturas válidas o con advertencias, sin repeticiones de un comprobante."""
    check_month(month)
    if fmt not in EXPORT_FORMATS:
        raise ConfigError(f"Formato de exportación desconocido: {fmt} (usa csv o xlsx)")
    chosen = list(statuses) if statuses else [ValidationStatus.VALID, ValidationStatus.WARNING]
    invalid = [s for s in chosen if s not in ValidationStatus.ALL]
    if invalid:
        raise ConfigError(
            f"Estado de validación desconocido: {', '.join(invalid)} (usa {', '.join(ValidationStatus.ALL)})"
        )
    if include_errors and ValidationStatus.ERROR not in chosen:
        chosen.append(ValidationStatus.ERROR)
    rows = deps.repository.list_invoices_for_export(
        month=month,
        validation_statuses=chosen,
        supplier_ruc=supplier_ruc,
        currency=currency.upper() if currency else None,
        include_duplicates=include_duplicates,
    )
    file_name = f"invoices-{month or 'all'}.{fmt}"
    if fmt == "xlsx":
        title = f"Facturas {month or '(todos los periodos)'} · estados: {', '.join(chosen)}"
        content = render_xlsx(rows, title=title)
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    else:
        content = render_csv(rows, delimiter=delimiter, bom=bom)
        media = "text/csv; charset=utf-8"
    return ExportResult(None, len(rows), content, file_name, media)


def export_invoices(deps: Deps, *, output: str | None = None, **options: Any) -> ExportResult:
    """Escribe la exportación en INVOICEBOT_EXPORT_DIR (o en `output`)."""
    result = build_export(deps, **options)
    target = Path(output) if output else Path(deps.config.export_dir) / result.file_name
    result.path = write_export(result.content, str(target.resolve()))
    return result


def find_duplicates(deps: Deps) -> dict[str, list[dict[str, Any]]]:
    """Duplicados exactos (mismo SHA-256) y posibles (misma clave de negocio). Nunca borra nada."""

    def group(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
        groups: dict[Any, list[dict[str, Any]]] = {}
        for row in rows:
            groups.setdefault(row[key], []).append(row)
        return [{"key": k, "items": v} for k, v in groups.items()]

    repo = deps.repository
    return {
        "exact": group(repo.list_exact_duplicates(), "document_id"),
        "possible": group(repo.list_possible_duplicates(), "business_key"),
    }
