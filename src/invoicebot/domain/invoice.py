"""Comprobante de pago: estados, catálogos SUNAT y normalización de datos extraídos."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from invoicebot.domain.ruc import normalize_ruc
from invoicebot.money import to_cents


class DocumentStatus:
    PENDING = "pending"
    PROCESSING = "processing"
    EXTRACTED = "extracted"
    FAILED = "failed"
    #: Sin texto suficiente: se procesará cuando haya OCR.
    NEEDS_OCR = "needs_ocr"
    #: No se procesa por diseño: representación impresa de un XML ya leído, CDR, etc.
    SKIPPED = "skipped"


class SkipReason:
    #: PDF con el mismo nombre que un XML ya extraído: sus datos vienen del XML.
    PRINTED_REPRESENTATION = "printed_representation"
    #: Constancia de recepción de SUNAT (ApplicationResponse).
    CDR = "cdr"
    #: XML que no es un comprobante UBL.
    UNSUPPORTED_XML = "unsupported_xml"


class DataSource:
    AI = "ai"
    XML = "xml"


class ValidationStatus:
    VALID = "valid"
    WARNING = "warning"
    ERROR = "error"

    ALL = ("valid", "warning", "error")


class Severity:
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


#: Catálogo 01 de SUNAT (tipos de comprobante más habituales).
DOCUMENT_TYPES: dict[str, str] = {
    "01": "Factura",
    "03": "Boleta de venta",
    "07": "Nota de crédito",
    "08": "Nota de débito",
    "12": "Ticket de máquina registradora",
    "14": "Recibo de servicios públicos",
    "91": "Comprobante de no domiciliado (proveedor del exterior)",
}

#: Comprobante de un proveedor del exterior: no tiene RUC, serie SUNAT ni IGV.
FOREIGN_DOCUMENT_TYPE = "91"

#: Campos monetarios del comprobante, en el orden en que se exportan.
AMOUNT_FIELDS: tuple[str, ...] = (
    "subtotal",
    "taxable_amount",
    "exempt_amount",
    "unaffected_amount",
    "discount_amount",
    "igv",
    "isc",
    "icbper",
    "other_charges",
    "other_taxes",
    "total",
    # Anticipos aplicados: el total ya los descuenta.
    "prepaid_amount",
    # Detracción a depositar en el Banco de la Nación. Siempre en soles.
    "detraction_amount",
)

TEXT_FIELDS: tuple[str, ...] = (
    "supplier_name",
    "supplier_ruc",
    "supplier_tax_id",
    "supplier_country",
    "customer_name",
    "customer_ruc",
    "document_type",
    "series",
    "number",
    "issue_date",
    "due_date",
    "currency",
    "detraction_code",
    "detraction_percent",
    "detraction_account",
)


@dataclass(frozen=True)
class InvoiceData:
    """Datos de un comprobante ya normalizados: textos limpios, fechas ISO, céntimos."""

    supplier_name: str | None = None
    supplier_ruc: str | None = None
    supplier_tax_id: str | None = None
    supplier_country: str | None = None
    customer_name: str | None = None
    customer_ruc: str | None = None
    document_type: str | None = None
    series: str | None = None
    number: str | None = None
    issue_date: str | None = None
    due_date: str | None = None
    currency: str | None = None
    detraction_code: str | None = None
    detraction_percent: float | None = None
    detraction_account: str | None = None
    subtotal_cents: int | None = None
    taxable_amount_cents: int | None = None
    exempt_amount_cents: int | None = None
    unaffected_amount_cents: int | None = None
    discount_amount_cents: int | None = None
    igv_cents: int | None = None
    isc_cents: int | None = None
    icbper_cents: int | None = None
    other_charges_cents: int | None = None
    other_taxes_cents: int | None = None
    total_cents: int | None = None
    prepaid_amount_cents: int | None = None
    detraction_amount_cents: int | None = None

    def get(self, column: str) -> Any:
        return getattr(self, column)

    def with_changes(self, **changes: Any) -> InvoiceData:
        return replace(self, **changes)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, row: Any) -> InvoiceData:
        """Factura desde una fila de la base de datos (fechas date, DECIMAL…)."""
        values: dict[str, Any] = {}
        for f in fields(cls):
            value = row.get(f.name) if hasattr(row, "get") else getattr(row, f.name, None)
            if isinstance(value, datetime):
                value = value.date().isoformat()
            elif isinstance(value, date):
                value = value.isoformat()
            elif isinstance(value, Decimal):
                value = float(value)
            if f.name.endswith("_cents") and value is not None:
                value = int(value)
            values[f.name] = value
        return cls(**values)


INVOICE_COLUMNS: tuple[str, ...] = tuple(f.name for f in fields(InvoiceData))


@dataclass(frozen=True)
class NewIssue:
    """Incidencia de validación tal como la generan las reglas, antes de guardarse."""

    rule_code: str
    severity: str
    message: str
    expected_value: str | None = None
    actual_value: str | None = None


@dataclass(frozen=True)
class NormalizationIssue:
    field: str
    value: str


@dataclass
class NormalizedInvoice:
    invoice: InvoiceData
    normalization_issues: list[NormalizationIssue] = field(default_factory=list)


def clean_text(value: object) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value).strip())
    return text or None


_MONTHS: dict[str, int] = {
    "jan": 1, "january": 1, "ene": 1, "enero": 1,
    "feb": 2, "february": 2, "febrero": 2,
    "mar": 3, "march": 3, "marzo": 3,
    "apr": 4, "april": 4, "abr": 4, "abril": 4,
    "may": 5, "mayo": 5,
    "jun": 6, "june": 6, "junio": 6,
    "jul": 7, "july": 7, "julio": 7,
    "aug": 8, "august": 8, "ago": 8, "agosto": 8,
    "sep": 9, "sept": 9, "september": 9, "set": 9, "septiembre": 9, "setiembre": 9,
    "oct": 10, "october": 10, "octubre": 10,
    "nov": 11, "november": 11, "noviembre": 11,
    "dec": 12, "december": 12, "dic": 12, "diciembre": 12,
}  # fmt: skip


def _iso(y: str | int, m: str | int, d: str | int) -> str:
    return f"{y}-{int(m):02d}-{int(d):02d}"


def normalize_date(value: object) -> str | None:
    """Acepta AAAA-MM-DD, DD/MM/AAAA (o DD-MM-AAAA) y el mes en letras.

    "August 5, 2025" o "5 de agosto de 2025". No valida el calendario: una
    fecha imposible se conserva para que la validación la marque.
    """
    text = clean_text(value)
    if text is None:
        return None
    if m := re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", text):
        return _iso(m[1], m[2], m[3])
    if m := re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$", text):
        return _iso(m[3], m[2], m[1])
    if (m := re.match(r"^([a-záé]+)\.? (\d{1,2}),? (\d{4})$", text, re.IGNORECASE)) and (
        month := _MONTHS.get(m[1].lower())
    ):
        return _iso(m[3], month, m[2])
    if (
        m := re.match(r"^(\d{1,2})(?: de)? ([a-záé]+)\.?(?: de)? (\d{4})$", text, re.IGNORECASE)
    ) and (month := _MONTHS.get(m[2].lower())):
        return _iso(m[3], month, m[1])
    return text


def normalize_document_type(value: object) -> str | None:
    text = clean_text(value)
    if text is None:
        return None
    if re.match(r"^\d{1,2}$", text):
        return text.zfill(2)
    lower = text.lower()
    if "nota de cr" in lower:
        return "07"
    if "nota de d" in lower:
        return "08"
    if "boleta" in lower:
        return "03"
    if "factura" in lower:
        return "01"
    return text


def normalize_currency(value: object) -> str | None:
    text = clean_text(value)
    if text is None:
        return None
    upper = text.upper()
    if upper in ("S/", "S/.", "SOLES"):
        return "PEN"
    if upper in ("US$", "$", "DOLARES", "DÓLARES"):
        return "USD"
    return upper


#: Nombres de país habituales en facturas de proveedores → código ISO 3166.
_COUNTRY_CODES: dict[str, str] = {
    "UNITED STATES": "US", "UNITED STATES OF AMERICA": "US", "USA": "US", "U.S.A.": "US",
    "U.S.": "US", "ESTADOS UNIDOS": "US", "EE. UU.": "US", "EE.UU.": "US",
    "IRELAND": "IE", "IRLANDA": "IE",
    "UNITED KINGDOM": "GB", "UK": "GB", "REINO UNIDO": "GB",
    "CANADA": "CA", "CANADÁ": "CA",
    "GERMANY": "DE", "ALEMANIA": "DE",
    "NETHERLANDS": "NL", "PAÍSES BAJOS": "NL",
    "SINGAPORE": "SG", "SINGAPUR": "SG",
    "MEXICO": "MX", "MÉXICO": "MX",
    "SPAIN": "ES", "ESPAÑA": "ES",
    "CHINA": "CN",
    "PERU": "PE", "PERÚ": "PE",
}  # fmt: skip


def normalize_country(value: object) -> str | None:
    """Código ISO de dos letras; un nombre de país conocido se convierte."""
    text = clean_text(value)
    if text is None:
        return None
    if re.match(r"^[a-z]{2}$", text, re.IGNORECASE):
        return text.upper()
    return _COUNTRY_CODES.get(text.upper(), text)


def normalize_detraction_code(value: object) -> str | None:
    """ "037", "37" o "Código 037" → "037"."""
    text = clean_text(value)
    if text is None:
        return None
    m = re.search(r"(\d{1,3})", text)
    return m[1].zfill(3) if m else None


def normalize_percent(value: object) -> float | None:
    """ "12", "12.00", "12 %" o "12,5" → 12.0 / 12.5. None si no es un porcentaje válido."""
    text = clean_text(value)
    if text is None:
        return None
    text = text.replace("%", "").replace(",", ".").strip()
    if not re.match(r"^\d{1,3}(\.\d{1,2})?$", text):
        return None
    n = float(text)
    return n if 0 < n <= 100 else None


def has_invoice_data(extraction: dict[str, Any]) -> bool:
    """¿La extracción tiene los datos mínimos de un comprobante?

    Tipo conocido, número, emisor identificado y total: sirve para no descartar
    un comprobante real solo porque el modelo dudó.
    """
    doc_type = normalize_document_type(extraction.get("document_type"))
    supplier = (
        extraction.get("supplier_ruc")
        or extraction.get("supplier_tax_id")
        or extraction.get("supplier_name")
    )
    return bool(
        doc_type
        and doc_type in DOCUMENT_TYPES
        and extraction.get("number")
        and supplier
        and extraction.get("total") is not None
    )


def normalize_invoice(extraction: dict[str, Any]) -> NormalizedInvoice:
    """Convierte la salida cruda del modelo (o del XML) en una factura normalizada.

    Nunca inventa valores: lo que no se puede interpretar queda como None y se
    informa en `normalization_issues` para que la validación lo marque.
    """
    issues: list[NormalizationIssue] = []
    amounts: dict[str, int | None] = {}
    for name in AMOUNT_FIELDS:
        raw = extraction.get(name)
        try:
            amounts[f"{name}_cents"] = to_cents(raw)
        except ValueError:
            amounts[f"{name}_cents"] = None
            issues.append(NormalizationIssue(name, str(raw)))

    percent = normalize_percent(extraction.get("detraction_percent"))
    if percent is None and clean_text(extraction.get("detraction_percent")) is not None:
        issues.append(
            NormalizationIssue("detraction_percent", str(extraction.get("detraction_percent")))
        )

    series = clean_text(extraction.get("series"))
    number = clean_text(extraction.get("number"))
    invoice = InvoiceData(
        supplier_name=clean_text(extraction.get("supplier_name")),
        supplier_ruc=normalize_ruc(extraction.get("supplier_ruc")),
        supplier_tax_id=clean_text(extraction.get("supplier_tax_id")),
        supplier_country=normalize_country(extraction.get("supplier_country")),
        customer_name=clean_text(extraction.get("customer_name")),
        customer_ruc=normalize_ruc(extraction.get("customer_ruc")),
        document_type=normalize_document_type(extraction.get("document_type")),
        series=series.upper() if series else None,
        number=re.sub(r"\s", "", number) if number else None,
        issue_date=normalize_date(extraction.get("issue_date")),
        due_date=normalize_date(extraction.get("due_date")),
        currency=normalize_currency(extraction.get("currency")),
        detraction_code=normalize_detraction_code(extraction.get("detraction_code")),
        detraction_percent=percent,
        detraction_account=clean_text(extraction.get("detraction_account")),
        **amounts,
    )
    return NormalizedInvoice(invoice, issues)
