"""Validación matemática del RUC peruano (11 dígitos, módulo 11).

Que un RUC pase esta validación NO confirma que el contribuyente exista, esté
activo o que el comprobante sea auténtico: para eso está la verificación en
SUNAT (`invoicebot verify-sunat`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_WEIGHTS = (5, 4, 3, 2, 7, 6, 5, 4, 3, 2)
#: 10 persona natural, 15/16/17 casos especiales, 20 persona jurídica.
_VALID_PREFIXES = frozenset({"10", "15", "16", "17", "20"})


def compute_ruc_check_digit(first_ten_digits: str) -> int:
    total = sum(w * int(d) for w, d in zip(_WEIGHTS, first_ten_digits, strict=False))
    digit = 11 - (total % 11)
    if digit == 10:
        return 0
    if digit == 11:
        return 1
    return digit


@dataclass(frozen=True)
class RucValidation:
    valid: bool
    reason: str | None = None  # format | prefix | check_digit
    expected: str | None = None


def validate_ruc(ruc: object) -> RucValidation:
    if not isinstance(ruc, str) or not re.fullmatch(r"\d{11}", ruc):
        return RucValidation(False, "format")
    if ruc[:2] not in _VALID_PREFIXES:
        return RucValidation(False, "prefix")
    expected = compute_ruc_check_digit(ruc[:10])
    if expected != int(ruc[10]):
        return RucValidation(False, "check_digit", str(expected))
    return RucValidation(True)


def normalize_ruc(value: object) -> str | None:
    """Elimina espacios, guiones y puntos que a veces acompañan al RUC impreso."""
    if value is None:
        return None
    cleaned = re.sub(r"[\s.\-]", "", str(value))
    return cleaned or None
