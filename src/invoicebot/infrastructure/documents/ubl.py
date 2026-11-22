"""Lector de comprobantes electrónicos SUNAT en UBL 2.1 (sin IA).

Factura, boleta, nota de crédito y de débito. Devuelve los datos con la misma
forma que la extracción por IA, para que pasen por la misma normalización y
validación. No verifica la firma digital: confirma la estructura y los datos,
no la autenticidad del documento. El XML se analiza con defusedxml (sin
entidades externas ni expansión exponencial).
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any
from xml.etree.ElementTree import Element

from defusedxml import ElementTree as SafeET

from invoicebot.errors import DocumentReadError
from invoicebot.infrastructure.ai.schema import Extraction
from invoicebot.money import format_cents, to_cents

#: Se guarda en extraction_runs.prompt_version: con qué lógica se leyó cada XML.
UBL_PARSER_VERSION = "ubl-2.1-sunat/2026-10-31.1"

_ROOT_DOCUMENT_TYPES: dict[str, str | None] = {
    "Invoice": None,
    "CreditNote": "07",
    "DebitNote": "08",
}

#: Catálogo 05 de SUNAT (tipos de tributo).
_IGV, _IVAP, _ISC, _ICBPER = "1000", "1016", "2000", "7152"
_EXPORT, _FREE, _EXEMPT, _UNAFFECTED, _OTHER = "9995", "9996", "9997", "9998", "9999"


@dataclass
class StructuredDocument:
    #: invoice | cdr | other
    kind: str
    data: dict[str, Any] = field(default_factory=dict)
    root: str | None = None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(node: Element | None, name: str) -> list[Element]:
    if node is None:
        return []
    return [c for c in node if _local(c.tag) == name]


def _child(node: Element | None, *path: str) -> Element | None:
    current = node
    for name in path:
        found = _children(current, name)
        if not found:
            return None
        current = found[0]
    return current


def _text(node: Element | None) -> str | None:
    if node is None or node.text is None:
        return None
    value = node.text.strip()
    return value or None


def decode_xml(data: bytes) -> str:
    """Respeta la codificación declarada (SUNAT admite UTF-8 e ISO-8859-1)."""
    prolog = data[:200].decode("latin-1")
    m = re.search(r"encoding\s*=\s*[\"']([^\"']+)[\"']", prolog, re.IGNORECASE)
    declared = (m[1] if m else "utf-8").lower()
    encoding = "latin-1" if declared in ("iso-8859-1", "latin1", "windows-1252") else "utf-8"
    text = data.decode(encoding, errors="replace")
    # ElementTree rechaza una declaración de codificación en un str.
    return re.sub(r"^﻿?\s*<\?xml[^>]*\?>", "", text, count=1)


def _party(root: Element, name: str) -> tuple[str | None, str | None]:
    party = _child(root, name, "Party")
    if party is None:
        return None, None
    legal = _child(party, "PartyLegalEntity")
    party_name = _text(_child(legal, "RegistrationName")) or _text(
        _child(party, "PartyName", "Name")
    )
    # Preferir el documento con schemeID 6 (RUC); si no hay, el primero (DNI = 1…).
    ids = [i for p in _children(party, "PartyIdentification") if (i := _child(p, "ID")) is not None]
    ruc = next((i for i in ids if i.get("schemeID") == "6"), None)
    return party_name, _text(ruc if ruc is not None else (ids[0] if ids else None))


def _sum_amounts(values: list[str | None]) -> str | None:
    """Suma importes; uno no numérico se devuelve tal cual para que la validación lo marque."""
    present = [v for v in values if v is not None]
    if len(present) <= 1:
        return present[0] if present else None
    try:
        return format_cents(sum(to_cents(v) or 0 for v in present))
    except ValueError:
        return " + ".join(present)


def _is_detraction(node: Element) -> bool:
    return bool(re.fullmatch(r"detracci[oó]n", _text(_child(node, "ID")) or "", re.IGNORECASE))


def parse_ubl(data: bytes) -> StructuredDocument:
    try:
        root = SafeET.fromstring(decode_xml(data))
    except Exception as error:
        raise DocumentReadError(f"XML mal formado: {error}") from error

    root_name = _local(root.tag)
    # Constancia de recepción de SUNAT: acompaña al comprobante, pero no lo es.
    if root_name == "ApplicationResponse":
        return StructuredDocument("cdr")
    if root_name not in _ROOT_DOCUMENT_TYPES:
        return StructuredDocument("other", root=root_name)

    notes: list[str] = []
    series, _, number = (_text(_child(root, "ID")) or "").partition("-")
    document_type = _ROOT_DOCUMENT_TYPES[root_name] or _text(_child(root, "InvoiceTypeCode"))
    supplier_name, supplier_id = _party(root, "AccountingSupplierParty")
    customer_name, customer_id = _party(root, "AccountingCustomerParty")
    totals = _child(root, "LegalMonetaryTotal")
    if totals is None:
        totals = _child(root, "RequestedMonetaryTotal")

    taxable_by: dict[str, list[str | None]] = defaultdict(list)
    taxes_by: dict[str, list[str | None]] = defaultdict(list)
    for total in _children(root, "TaxTotal"):
        for sub in _children(total, "TaxSubtotal"):
            scheme = _text(_child(sub, "TaxCategory", "TaxScheme", "ID"))
            if not scheme:
                continue
            taxable_by[scheme].append(_text(_child(sub, "TaxableAmount")))
            taxes_by[scheme].append(_text(_child(sub, "TaxAmount")))

    def taxable(code: str) -> str | None:
        return _sum_amounts(taxable_by.get(code, []))

    def tax(code: str) -> str | None:
        return _sum_amounts(taxes_by.get(code, []))

    # Las exportaciones no llevan IGV: para la aritmética se agrupan con las inafectas.
    unaffected = _sum_amounts([taxable(_UNAFFECTED), taxable(_EXPORT)])
    if _EXPORT in taxable_by:
        notes.append("Incluye operaciones de exportación (sumadas a inafectas).")
    if _FREE in taxable_by:
        notes.append("Incluye operaciones gratuitas, que no forman parte del total.")
    if _IVAP in taxable_by:
        notes.append("Incluye IVAP (registrado en otros tributos).")

    payment_terms = _children(root, "PaymentTerms")
    terms = next((t for t in payment_terms if _is_detraction(t)), None)
    means = next((m for m in _children(root, "PaymentMeans") if _is_detraction(m)), None)
    detraction: dict[str, str | None] = {
        "detraction_code": None,
        "detraction_percent": None,
        "detraction_amount": None,
        "detraction_account": None,
    }
    if terms is not None or means is not None:
        amount_node = _child(terms, "Amount")
        currency = amount_node.get("currencyID") if amount_node is not None else None
        if currency and currency != "PEN":
            notes.append(f"El monto de detracción viene en {currency}; SUNAT lo exige en soles.")
        detraction = {
            "detraction_code": _text(_child(terms, "PaymentMeansID")),
            "detraction_percent": _text(_child(terms, "PaymentPercent")),
            "detraction_amount": _text(amount_node),
            "detraction_account": _text(_child(means, "PayeeFinancialAccount", "ID")),
        }

    due_date = _text(_child(root, "DueDate"))
    if due_date is None:
        # Al crédito, la primera cuota (PaymentTerms "FormaPago") lleva el vencimiento.
        due_date = next(
            (d for t in payment_terms if (d := _text(_child(t, "PaymentDueDate")))), None
        )

    fields: dict[str, Any] = {
        "is_invoice": True,
        "supplier_name": supplier_name,
        "supplier_ruc": supplier_id,
        "supplier_tax_id": None,
        "supplier_country": "PE",
        "customer_name": customer_name,
        "customer_ruc": customer_id,
        "document_type": document_type,
        "series": series.strip() or None,
        "number": number.strip() or None,
        "issue_date": _text(_child(root, "IssueDate")),
        "due_date": due_date,
        "currency": _text(_child(root, "DocumentCurrencyCode")),
        "subtotal": _text(_child(totals, "LineExtensionAmount")),
        "taxable_amount": taxable(_IGV),
        "exempt_amount": taxable(_EXEMPT),
        "unaffected_amount": unaffected,
        "discount_amount": _text(_child(totals, "AllowanceTotalAmount")),
        "igv": tax(_IGV),
        "isc": tax(_ISC),
        "icbper": tax(_ICBPER),
        "other_charges": _text(_child(totals, "ChargeTotalAmount")),
        "other_taxes": _sum_amounts([tax(_OTHER), tax(_IVAP)]),
        "total": _text(_child(totals, "PayableAmount")),
        "prepaid_amount": _text(_child(totals, "PrepaidAmount")),
        **detraction,
        "notes": notes,
    }
    return StructuredDocument("invoice", Extraction.model_validate(fields).model_dump())
