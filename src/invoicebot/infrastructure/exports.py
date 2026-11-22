"""Exportación de facturas a CSV y Excel (.xlsx).

Las columnas son parte del contrato público (docs/csv-format.md): las nuevas se
añaden al final y las existentes no se renombran.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from invoicebot.domain.detraction import net_payable_cents, total_in_pen
from invoicebot.domain.invoice import InvoiceData
from invoicebot.money import format_cents


@dataclass(frozen=True)
class ExportColumn:
    key: str
    label: str
    #: id | text | date | amount | int | decimal
    type: str
    width: int | None = None


EXPORT_COLUMNS: tuple[ExportColumn, ...] = (
    ExportColumn("invoice_id", "ID", "id", 7),
    ExportColumn("issue_date", "Fecha de emisión", "date", 12),
    ExportColumn("due_date", "Vencimiento", "date", 12),
    ExportColumn("supplier_ruc", "RUC emisor", "text", 13),
    ExportColumn("supplier_name", "Emisor", "text", 34),
    ExportColumn("customer_ruc", "RUC/DNI cliente", "text", 13),
    ExportColumn("customer_name", "Cliente", "text", 28),
    ExportColumn("document_type", "Tipo", "text", 6),
    ExportColumn("series", "Serie", "text", 7),
    ExportColumn("number", "Número", "text", 11),
    ExportColumn("currency", "Moneda", "text", 8),
    ExportColumn("subtotal", "Subtotal", "amount"),
    ExportColumn("taxable_amount", "Op. gravadas", "amount"),
    ExportColumn("exempt_amount", "Op. exoneradas", "amount"),
    ExportColumn("unaffected_amount", "Op. inafectas", "amount"),
    ExportColumn("discount_amount", "Descuentos", "amount"),
    ExportColumn("igv", "IGV", "amount"),
    ExportColumn("isc", "ISC", "amount"),
    ExportColumn("icbper", "ICBPER", "amount"),
    ExportColumn("other_charges", "Otros cargos", "amount"),
    ExportColumn("other_taxes", "Otros tributos", "amount"),
    ExportColumn("total", "Total", "amount"),
    ExportColumn("validation_status", "Validación", "text", 11),
    ExportColumn("duplicate_count", "Duplicados", "int", 10),
    ExportColumn("file_name", "Archivo", "text", 34),
    ExportColumn("sha256", "SHA-256", "text", 20),
    ExportColumn("data_source", "Origen", "text", 8),
    ExportColumn("sunat_status", "Estado SUNAT", "text", 14),
    ExportColumn("prepaid_amount", "Anticipos", "amount"),
    ExportColumn("supplier_tax_id", "ID fiscal emisor (exterior)", "text", 16),
    ExportColumn("supplier_country", "País emisor", "text", 8),
    ExportColumn("review_status", "Revisión", "text", 10),
    ExportColumn("detraction_code", "Cód. detracción", "text", 9),
    ExportColumn("detraction_percent", "% detracción", "decimal", 9),
    ExportColumn("detraction_amount", "Detracción (S/)", "amount"),
    ExportColumn("detraction_account", "Cta. detracciones", "text", 16),
    ExportColumn("exchange_rate", "Tipo de cambio", "decimal", 9),
    ExportColumn("exchange_rate_date", "Fecha T.C.", "date", 12),
    ExportColumn("total_pen", "Total en soles", "amount"),
    ExportColumn("net_payable", "Neto a pagar", "amount"),
)

_AMOUNT_FORMAT = "#,##0.00"
_DATE_FORMAT = "dd/mm/yyyy"


def _rate(row: dict[str, Any]) -> float | None:
    value = row.get("exchange_rate")
    return None if value is None else float(value)


def column_value(row: dict[str, Any], column: ExportColumn) -> Any:
    """Valor de una columna (los importes, en céntimos)."""
    if column.key == "invoice_id":
        return row.get("id")
    if column.key == "total_pen":
        return total_in_pen(InvoiceData.from_mapping(row), _rate(row))
    if column.key == "net_payable":
        return net_payable_cents(InvoiceData.from_mapping(row), _rate(row))
    if column.type == "amount":
        return row.get(f"{column.key}_cents")
    return row.get(column.key)


def _csv_text(value: Any, column: ExportColumn) -> str:
    if value is None:
        return ""
    if column.type == "amount":
        return format_cents(int(value))
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    return str(value)


def render_csv(rows: Sequence[dict[str, Any]], *, delimiter: str = ",", bom: bool = False) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(
        buffer, delimiter=delimiter, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL
    )
    writer.writerow([c.key for c in EXPORT_COLUMNS])
    for row in rows:
        writer.writerow([_csv_text(column_value(row, c), c) for c in EXPORT_COLUMNS])
    data = buffer.getvalue().encode("utf-8")
    return (b"\xef\xbb\xbf" + data) if bom else data


def _summarize(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (row.get("currency") or "—", str(row.get("validation_status")))
        g = groups.setdefault(
            key,
            {"currency": key[0], "status": key[1], "count": 0, "taxable": 0, "igv": 0, "total": 0},
        )
        g["count"] += 1
        g["taxable"] += row.get("taxable_amount_cents") or 0
        g["igv"] += row.get("igv_cents") or 0
        g["total"] += row.get("total_cents") or 0
    return [groups[k] for k in sorted(groups)]


def render_xlsx(rows: Sequence[dict[str, Any]], *, title: str | None = None) -> bytes:
    """Hoja "Facturas" con importes numéricos y fechas reales, y hoja "Resumen"."""
    wb = Workbook()
    sheet = wb.active
    assert sheet is not None
    sheet.title = "Facturas"
    header_font = Font(bold=True)
    header_fill = PatternFill("solid", fgColor="DDEBF7")
    for index, column in enumerate(EXPORT_COLUMNS, start=1):
        cell = sheet.cell(row=1, column=index, value=column.label)
        cell.font, cell.fill = header_font, header_fill
        cell.alignment = Alignment(wrap_text=True, vertical="top")
        sheet.column_dimensions[get_column_letter(index)].width = column.width or (
            12 if column.type == "amount" else 14
        )
    for r, row in enumerate(rows, start=2):
        for c, column in enumerate(EXPORT_COLUMNS, start=1):
            value = column_value(row, column)
            if value is None or value == "":
                continue
            cell = sheet.cell(row=r, column=c)
            if column.type == "amount":
                cell.value = int(value) / 100
                cell.number_format = _AMOUNT_FORMAT
            elif column.type == "date":
                if isinstance(value, datetime):
                    value = value.date()
                if isinstance(value, str):
                    try:
                        value = date.fromisoformat(value)
                    except ValueError:
                        cell.value = value
                        continue
                cell.value = value
                cell.number_format = _DATE_FORMAT
            elif column.type in ("id", "int", "decimal"):
                cell.value = float(value) if column.type == "decimal" else int(value)
            else:
                # RUC, serie y número como texto: no deben perder ceros.
                cell.value = str(value)
                cell.number_format = "@"
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = f"A1:{get_column_letter(len(EXPORT_COLUMNS))}{max(len(rows) + 1, 1)}"

    summary = wb.create_sheet("Resumen")
    line = 1
    if title:
        summary.cell(row=1, column=1, value=title).font = header_font
        line = 3
    for index, label in enumerate(
        ("Moneda", "Validación", "Comprobantes", "Op. gravadas", "IGV", "Total"), start=1
    ):
        cell = summary.cell(row=line, column=index, value=label)
        cell.font, cell.fill = header_font, header_fill
        summary.column_dimensions[get_column_letter(index)].width = 14
    for offset, group in enumerate(_summarize(rows), start=1):
        values = [
            group["currency"],
            group["status"],
            group["count"],
            group["taxable"] / 100,
            group["igv"] / 100,
            group["total"] / 100,
        ]
        for index, value in enumerate(values, start=1):
            cell = summary.cell(row=line + offset, column=index, value=value)
            if index >= 4:
                cell.number_format = _AMOUNT_FORMAT
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def write_export(content: bytes, output_path: str) -> str:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(content)
    tmp.replace(path)  # Quien tenga el Excel abierto no ve un archivo a medio escribir.
    return str(path)
