"""Reglas de validación contable de un comprobante normalizado.

Funciones puras: no consultan la base de datos ni la fecha del sistema (se
recibe `today` para que el resultado sea reproducible).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from datetime import date

from invoicebot.domain.detraction import check_detraction
from invoicebot.domain.exchange_rate import ExchangeRate, check_exchange_rate
from invoicebot.domain.invoice import (
    AMOUNT_FIELDS,
    DOCUMENT_TYPES,
    FOREIGN_DOCUMENT_TYPE,
    InvoiceData,
    NewIssue,
    NormalizationIssue,
    Severity,
    ValidationStatus,
)
from invoicebot.domain.ruc import validate_ruc
from invoicebot.money import apply_rate_basis_points, format_cents, sum_cents

#: Versión de las reglas. Se guarda con cada factura para poder auditar resultados.
VALIDATION_RULES_VERSION = "2026.10.3"

#: 18 % general (16 % IGV + 2 % IPM) y 10,5 % de restaurantes y alojamientos (Ley 31556).
DEFAULT_IGV_RATES_BP: tuple[int, ...] = (1800, 1050)

_KNOWN_CURRENCIES = ("PEN", "USD", "EUR")
_REQUIRED_FIELDS = (
    "supplier_ruc",
    "document_type",
    "series",
    "number",
    "issue_date",
    "currency",
    "total_cents",
)
#: Un proveedor del exterior se identifica por su nombre o su identificador fiscal.
_FOREIGN_REQUIRED_FIELDS = ("document_type", "number", "issue_date", "currency", "total_cents")

#: Sentinela: "no se comprobó el tipo de cambio" (distinto de None = "se buscó y no hay").
NOT_CHECKED: object = object()


def _issue(
    rule_code: str,
    severity: str,
    message: str,
    *,
    expected: object = None,
    actual: object = None,
) -> NewIssue:
    return NewIssue(
        rule_code,
        severity,
        message,
        None if expected is None else str(expected),
        None if actual is None else str(actual),
    )


def _is_foreign(invoice: InvoiceData) -> bool:
    return invoice.document_type == FOREIGN_DOCUMENT_TYPE


def is_calendar_date(text: str) -> bool:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return False
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return True


def _check_required(invoice: InvoiceData) -> list[NewIssue]:
    names = _FOREIGN_REQUIRED_FIELDS if _is_foreign(invoice) else _REQUIRED_FIELDS
    issues = [
        _issue(
            "REQUIRED_FIELD_MISSING",
            Severity.ERROR,
            f"Falta el campo obligatorio {name.removesuffix('_cents')}",
            actual="null",
        )
        for name in names
        if invoice.get(name) is None
    ]
    if _is_foreign(invoice) and not invoice.supplier_name and not invoice.supplier_tax_id:
        issues.append(
            _issue(
                "REQUIRED_FIELD_MISSING",
                Severity.ERROR,
                "Falta identificar al proveedor del exterior (nombre o identificador fiscal)",
                actual="null",
            )
        )
    return issues


def _check_rucs(invoice: InvoiceData) -> list[NewIssue]:
    issues: list[NewIssue] = []
    if _is_foreign(invoice) and invoice.supplier_ruc:
        # Un emisor con RUC es peruano: probablemente el tipo de comprobante está mal.
        issues.append(
            _issue(
                "FOREIGN_WITH_RUC",
                Severity.WARNING,
                "El comprobante figura como del exterior pero el emisor tiene RUC peruano",
                actual=invoice.supplier_ruc,
            )
        )
    checks: list[tuple[str, str, str, str]] = []
    if not _is_foreign(invoice):
        checks.append(("supplier_ruc", "SUPPLIER_RUC", Severity.ERROR, "emisor"))
    checks.append(("customer_ruc", "CUSTOMER_RUC", Severity.WARNING, "cliente"))
    for field_name, prefix, severity, label in checks:
        ruc = invoice.get(field_name)
        if not ruc:
            continue
        # Un DNI (8 dígitos) es válido como documento del cliente en boletas.
        if field_name == "customer_ruc" and re.fullmatch(r"\d{8}", ruc):
            continue
        result = validate_ruc(ruc)
        if result.valid:
            continue
        if result.reason == "check_digit":
            issues.append(
                _issue(
                    f"{prefix}_CHECK_DIGIT",
                    severity,
                    f"El dígito de control del RUC del {label} no es correcto",
                    expected=result.expected,
                    actual=ruc[10],
                )
            )
        else:
            issues.append(
                _issue(
                    f"{prefix}_FORMAT",
                    severity,
                    f"El RUC del {label} debe tener 11 dígitos y un prefijo válido "
                    "(10, 15, 16, 17, 20)",
                    actual=ruc,
                )
            )
    return issues


def _check_dates(invoice: InvoiceData, today: str) -> list[NewIssue]:
    issues: list[NewIssue] = []
    issue_date, due_date = invoice.issue_date, invoice.due_date
    if issue_date:
        if not is_calendar_date(issue_date):
            issues.append(
                _issue(
                    "ISSUE_DATE_INVALID",
                    Severity.ERROR,
                    "La fecha de emisión no es una fecha válida (AAAA-MM-DD)",
                    actual=issue_date,
                )
            )
        elif issue_date > today:
            issues.append(
                _issue(
                    "ISSUE_DATE_FUTURE",
                    Severity.WARNING,
                    "La fecha de emisión es posterior a hoy",
                    expected=f"<= {today}",
                    actual=issue_date,
                )
            )
        elif issue_date < "2000-01-01":
            issues.append(
                _issue(
                    "ISSUE_DATE_TOO_OLD",
                    Severity.WARNING,
                    "La fecha de emisión es anterior al año 2000",
                    actual=issue_date,
                )
            )
    if due_date:
        if not is_calendar_date(due_date):
            issues.append(
                _issue(
                    "DUE_DATE_INVALID",
                    Severity.WARNING,
                    "La fecha de vencimiento no es una fecha válida",
                    actual=due_date,
                )
            )
        elif issue_date and is_calendar_date(issue_date) and due_date < issue_date:
            issues.append(
                _issue(
                    "DUE_DATE_BEFORE_ISSUE",
                    Severity.WARNING,
                    "La fecha de vencimiento es anterior a la de emisión",
                    expected=f">= {issue_date}",
                    actual=due_date,
                )
            )
    return issues


def _check_identification(invoice: InvoiceData) -> list[NewIssue]:
    issues: list[NewIssue] = []
    doc_type, series, number, currency = (
        invoice.document_type,
        invoice.series,
        invoice.number,
        invoice.currency,
    )
    if not invoice.supplier_name:
        issues.append(
            _issue(
                "SUPPLIER_NAME_MISSING",
                Severity.WARNING,
                "No se identificó la razón social del emisor",
            )
        )
    if doc_type and doc_type not in DOCUMENT_TYPES:
        issues.append(
            _issue(
                "DOCUMENT_TYPE_UNKNOWN",
                Severity.WARNING,
                "Tipo de comprobante fuera del catálogo soportado",
                expected=",".join(DOCUMENT_TYPES),
                actual=doc_type,
            )
        )
    if _is_foreign(invoice):
        issues.append(
            _issue(
                "FOREIGN_SUPPLIER",
                Severity.INFO,
                "Comprobante de un proveedor del exterior (no domiciliado): revisa con tu "
                "contador el IGV por utilización de servicios y la retención del impuesto a "
                "la renta",
                actual=" · ".join(
                    v for v in (invoice.supplier_name, invoice.supplier_country) if v
                ),
            )
        )
        if number and not re.fullmatch(r"[A-Z0-9][A-Z0-9\-_/.]{0,39}", number, re.IGNORECASE):
            issues.append(
                _issue(
                    "NUMBER_FORMAT",
                    Severity.WARNING,
                    "Número de factura con caracteres inesperados",
                    actual=number,
                )
            )
        if currency and not re.fullmatch(r"[A-Z]{3}", currency):
            issues.append(
                _issue(
                    "CURRENCY_INVALID",
                    Severity.ERROR,
                    "La moneda debe ser un código ISO 4217 de 3 letras",
                    actual=currency,
                )
            )
        # Serie, correlativo SUNAT y monedas "peruanas" no aplican al exterior.
        return issues
    if series:
        # Electrónica: letra + 3 alfanuméricos (F001, B001, E001). Física: 4 dígitos.
        if not re.fullmatch(r"[FBE][A-Z0-9]{3}", series) and not re.fullmatch(r"\d{4}", series):
            issues.append(
                _issue(
                    "SERIES_FORMAT",
                    Severity.WARNING,
                    "La serie no sigue el formato SUNAT (F001, B001, E001 o 0001)",
                    actual=series,
                )
            )
        elif doc_type == "01" and series.startswith("B"):
            issues.append(
                _issue(
                    "SERIES_TYPE_MISMATCH",
                    Severity.WARNING,
                    "Una factura no debería usar una serie de boleta (B)",
                    actual=series,
                )
            )
        elif doc_type == "03" and series.startswith("F"):
            issues.append(
                _issue(
                    "SERIES_TYPE_MISMATCH",
                    Severity.WARNING,
                    "Una boleta no debería usar una serie de factura (F)",
                    actual=series,
                )
            )
    if number and not re.fullmatch(r"\d{1,8}", number):
        issues.append(
            _issue(
                "NUMBER_FORMAT",
                Severity.WARNING,
                "El correlativo debe tener entre 1 y 8 dígitos",
                actual=number,
            )
        )
    if currency:
        if not re.fullmatch(r"[A-Z]{3}", currency):
            issues.append(
                _issue(
                    "CURRENCY_INVALID",
                    Severity.ERROR,
                    "La moneda debe ser un código ISO 4217 de 3 letras",
                    actual=currency,
                )
            )
        elif currency not in _KNOWN_CURRENCIES:
            issues.append(
                _issue(
                    "CURRENCY_UNUSUAL",
                    Severity.WARNING,
                    "Moneda poco habitual para comprobantes peruanos",
                    expected=",".join(_KNOWN_CURRENCIES),
                    actual=currency,
                )
            )
    return issues


def _check_amounts(
    invoice: InvoiceData,
    normalization_issues: Sequence[NormalizationIssue],
    tolerance: int,
    igv_rates_bp: Sequence[int],
) -> list[NewIssue]:
    issues: list[NewIssue] = [
        _issue(
            "AMOUNT_UNPARSEABLE",
            Severity.ERROR,
            f"No se pudo interpretar el importe {n.field}",
            actual=n.value,
        )
        for n in normalization_issues
    ]
    for name in AMOUNT_FIELDS:
        cents = invoice.get(f"{name}_cents")
        if cents is not None and cents < 0:
            issues.append(
                _issue(
                    "AMOUNT_NEGATIVE",
                    Severity.ERROR,
                    f"El importe {name} es negativo",
                    expected=">= 0",
                    actual=format_cents(cents),
                )
            )

    subtotal = invoice.subtotal_cents
    taxable = invoice.taxable_amount_cents
    exempt = invoice.exempt_amount_cents
    unaffected = invoice.unaffected_amount_cents
    discount = invoice.discount_amount_cents
    igv = invoice.igv_cents
    total = invoice.total_cents
    # Anticipos ya pagados y aplicados a este comprobante: se restan del total.
    prepaid = invoice.prepaid_amount_cents or 0

    if total == 0 and prepaid == 0:
        issues.append(
            _issue(
                "TOTAL_ZERO",
                Severity.WARNING,
                "El importe total es cero (¿operación gratuita?)",
            )
        )

    # Base imponible: el desglose tributario si existe; si no, el subtotal.
    has_breakdown = any(v is not None for v in (taxable, exempt, unaffected))
    base = sum_cents(taxable, exempt, unaffected) if has_breakdown else subtotal
    charges = sum_cents(
        igv,
        invoice.isc_cents,
        invoice.icbper_cents,
        invoice.other_charges_cents,
        invoice.other_taxes_cents,
    )

    if total is not None and (total > 0 or prepaid > 0):
        if base is None:
            issues.append(
                _issue(
                    "TOTAL_NOT_VERIFIABLE",
                    Severity.WARNING,
                    "No hay subtotal ni desglose tributario para comprobar el total",
                )
            )
        else:
            expected = base + charges - prepaid
            # Descuentos globales y anticipos pueden venir ya restados de la base o
            # restarse después: se aceptan todas las lecturas coherentes.
            discounts = [0] if discount is None else [0, discount]
            prepaids = [prepaid, 0] if prepaid else [0]
            fits = any(
                abs(total - (base + charges - d - p)) <= tolerance
                for d in discounts
                for p in prepaids
            )
            if not fits:
                issues.append(
                    _issue(
                        "TOTAL_MISMATCH",
                        Severity.ERROR,
                        "El total no coincide con la base más los impuestos y cargos, menos "
                        "los anticipos"
                        if prepaid
                        else "El total no coincide con la base más los impuestos y cargos",
                        expected=format_cents(expected),
                        actual=format_cents(total),
                    )
                )

    if has_breakdown and subtotal is not None:
        breakdown = sum_cents(taxable, exempt, unaffected)
        ok = abs(subtotal - breakdown) <= tolerance or (
            discount is not None and abs(subtotal - discount - breakdown) <= tolerance
        )
        if not ok:
            issues.append(
                _issue(
                    "SUBTOTAL_MISMATCH",
                    Severity.WARNING,
                    "El subtotal no coincide con la suma de operaciones gravadas, exoneradas "
                    "e inafectas",
                    expected=format_cents(breakdown),
                    actual=format_cents(subtotal),
                )
            )

    if _is_foreign(invoice):
        # Un proveedor del exterior no cobra IGV; su IVA o VAT va en otros tributos.
        if igv is not None and igv > 0:
            issues.append(
                _issue(
                    "IGV_ON_FOREIGN_INVOICE",
                    Severity.WARNING,
                    "Un comprobante del exterior no debería traer IGV peruano (¿es VAT u otro "
                    "impuesto?)",
                    actual=format_cents(igv),
                )
            )
        return issues

    # El IGV solo se aplica a la base gravada. Sin desglose, usar el subtotal es
    # una suposición (puede haber exoneradas), por eso solo se advierte.
    igv_base = taxable if taxable is not None else (None if has_breakdown else subtotal)
    if igv is not None and igv_base is not None:
        if igv_base == 0 and igv > 0:
            issues.append(
                _issue(
                    "IGV_WITHOUT_TAXABLE_BASE",
                    Severity.WARNING,
                    "Hay IGV pero la base gravada es cero",
                    actual=format_cents(igv),
                )
            )
        elif igv_base > 0:
            fits = any(
                abs(apply_rate_basis_points(igv_base, bp) - igv) <= tolerance for bp in igv_rates_bp
            )
            if not fits:
                rates = ", ".join(f"{bp / 100:g} %" for bp in igv_rates_bp)
                issues.append(
                    _issue(
                        "IGV_RATE_MISMATCH",
                        Severity.WARNING,
                        f"El IGV no corresponde a ninguna tasa configurada ({rates}) sobre la "
                        "base gravada",
                        expected=format_cents(apply_rate_basis_points(igv_base, igv_rates_bp[0])),
                        actual=format_cents(igv),
                    )
                )
    elif igv is None and taxable is not None and taxable > 0:
        issues.append(
            _issue(
                "IGV_MISSING",
                Severity.WARNING,
                "Hay base gravada pero no se identificó el IGV",
            )
        )
    return issues


def exchange_dependent_issues(
    invoice: InvoiceData, exchange_rate: ExchangeRate | object | None
) -> list[NewIssue]:
    """Incidencias FX_* y DETRACTION_*: se recalculan al cargar el tipo de cambio."""
    issues: list[NewIssue] = []
    rate: ExchangeRate | None = exchange_rate if isinstance(exchange_rate, ExchangeRate) else None
    if exchange_rate is not NOT_CHECKED:
        issues.extend(check_exchange_rate(invoice, rate))
    issues.extend(check_detraction(invoice, exchange_rate=rate.rate if rate else None))
    return issues


def status_from_issues(issues: Iterable[object]) -> str:
    severities = {getattr(i, "severity", None) or _mapping_get(i, "severity") for i in issues}
    if Severity.ERROR in severities:
        return ValidationStatus.ERROR
    if Severity.WARNING in severities:
        return ValidationStatus.WARNING
    return ValidationStatus.VALID


def _mapping_get(item: object, key: str) -> object:
    getter: Callable[[str], object] | None = getattr(item, "get", None)
    return getter(key) if getter else None


def validate_invoice(
    invoice: InvoiceData,
    *,
    normalization_issues: Sequence[NormalizationIssue] = (),
    tolerance_cents: int = 2,
    today: str | None = None,
    igv_rates_bp: Sequence[int] = DEFAULT_IGV_RATES_BP,
    exchange_rate: ExchangeRate | object | None = NOT_CHECKED,
) -> tuple[str, list[NewIssue]]:
    """Valida una factura normalizada.

    `exchange_rate`: tipo de cambio de la fecha de emisión para comprobantes en
    otra moneda. None: se buscó y no hay (advertencia FX_RATE_MISSING). Sin
    indicar: no se comprueba.
    """
    today = today or date.today().isoformat()
    issues = [
        *_check_required(invoice),
        *_check_rucs(invoice),
        *_check_dates(invoice, today),
        *_check_identification(invoice),
        *_check_amounts(invoice, normalization_issues, tolerance_cents, igv_rates_bp),
        *exchange_dependent_issues(invoice, exchange_rate),
    ]
    return status_from_issues(issues), issues
