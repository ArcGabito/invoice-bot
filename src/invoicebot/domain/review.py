"""Revisión humana: estados y correcciones de datos."""

from __future__ import annotations

from dataclasses import dataclass

from invoicebot.domain.invoice import AMOUNT_FIELDS, TEXT_FIELDS, normalize_invoice
from invoicebot.money import format_cents, to_cents


class ReviewStatus:
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"

    ALL = ("pending", "approved", "rejected")


CORRECTABLE_FIELDS: tuple[str, ...] = (*TEXT_FIELDS, *AMOUNT_FIELDS)

#: Corregir uno de estos datos invalida la última consulta a SUNAT.
SUNAT_RELEVANT_FIELDS = frozenset(
    {"supplier_ruc", "document_type", "series", "number", "issue_date", "total"}
)


class CorrectionError(ValueError):
    pass


@dataclass(frozen=True)
class Correction:
    field: str
    column: str
    value: str | int | float | None
    display: str


def parse_correction(field_name: str, raw: object) -> Correction:
    """Convierte una corrección escrita por una persona en el valor que se guarda.

    Aplica la misma normalización que a los datos extraídos ("total=1,180.50",
    "issue_date=15/10/2026"). Un valor vacío borra el dato.
    """
    if field_name not in CORRECTABLE_FIELDS:
        raise CorrectionError(
            f'No se puede corregir "{field_name}". Campos permitidos: '
            + ", ".join(CORRECTABLE_FIELDS)
        )
    text = "" if raw is None else str(raw).strip()
    if field_name in AMOUNT_FIELDS:
        try:
            cents = None if text == "" else to_cents(text)
        except ValueError as error:
            raise CorrectionError(f'Importe no válido para {field_name}: "{raw}"') from error
        return Correction(
            field_name,
            f"{field_name}_cents",
            cents,
            "" if cents is None else format_cents(cents),
        )
    # Misma normalización para la IA y para las personas.
    invoice = normalize_invoice({field_name: None if text == "" else text}).invoice
    value = invoice.get(field_name)
    if field_name == "detraction_percent" and text != "" and value is None:
        raise CorrectionError(
            f'Porcentaje no válido para {field_name}: "{raw}" (usa p. ej. 12 o 1.5)'
        )
    return Correction(field_name, field_name, value, "" if value is None else _display(value))


def _display(value: object) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def split_assignment(assignment: str) -> tuple[str, str]:
    """ "total=118.00" → ("total", "118.00"). El valor puede contener "="."""
    index = assignment.find("=")
    if index <= 0:
        raise CorrectionError(f'Usa campo=valor (recibido: "{assignment}")')
    return assignment[:index].strip(), assignment[index + 1 :]


def is_preserved_on_revalidation(rule_code: str) -> bool:
    """Incidencias que no salen de las reglas y se conservan al revalidar.

    Las de SUNAT y las notas del modelo o del XML. Los posibles duplicados sí se
    recalculan: la corrección puede cambiar emisor, serie o número.
    """
    return rule_code.startswith("SUNAT_") or rule_code in {
        "MODEL_NOTE",
        "XML_NOTE",
        "MODEL_DOUBTED_INVOICE",
    }
