"""Importes monetarios como enteros en céntimos.

Nunca se opera con coma flotante: los valores se convierten desde su
representación decimal en texto, con redondeo comercial (mitad hacia arriba,
alejándose de cero).
"""

from __future__ import annotations

import math
import re
from decimal import Decimal

_DECIMAL = re.compile(r"^(-)?(\d+)(?:\.(\d+))?$")
_PREFIX = re.compile(r"^(S/\.?|US\$|USD|PEN|EUR|\$|€)", re.IGNORECASE)
_SUFFIX = re.compile(r"(S/\.?|USD|PEN|EUR)$", re.IGNORECASE)
#: BIGINT de SQL Server.
_MAX_CENTS = 2**63 - 1


def normalize_decimal_string(value: object) -> str | None:
    """Normaliza un importe escrito por una persona o un modelo a "1234.56".

    Acepta "S/ 1,234.56", "1.234,56", "US$ 10" o "(15.00)". Devuelve None si el
    valor está vacío y lanza ValueError si no es interpretable.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"Importe no interpretable: {value!r}")
    if isinstance(value, int):
        value = str(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"Importe no finito: {value}")
        # repr es la representación decimal más corta que identifica al número.
        value = repr(value)
    elif isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"Importe no finito: {value}")
        value = format(value, "f")
    if not isinstance(value, str):
        raise ValueError(f"Importe no interpretable: {value!r}")

    original = value
    text = re.sub(r"\s+", "", value)
    if text in ("", "-"):
        return None

    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1]
    text = _SUFFIX.sub("", _PREFIX.sub("", text))
    if text.startswith("-"):
        negative = not negative
        text = text[1:]

    last_dot = text.rfind(".")
    last_comma = text.rfind(",")
    if last_dot != -1 and last_comma != -1:
        # El separador que aparece al final es el decimal.
        decimal_sep = "." if last_dot > last_comma else ","
        thousand_sep = "," if decimal_sep == "." else "."
        text = text.replace(thousand_sep, "").replace(decimal_sep, ".")
    elif last_comma != -1:
        parts = text.split(",")
        # "1,234" o "1,234,567" → miles; "12,5" o "12,50" → decimal.
        looks_like_thousands = len(parts) > 2 or (len(parts) == 2 and len(parts[1]) == 3)
        text = "".join(parts) if looks_like_thousands else ".".join(parts)
    elif last_dot != -1 and text.count(".") > 1:
        # "1.234.567" → separador de miles europeo.
        text = text.replace(".", "")

    match = _DECIMAL.match(text)
    if not match:
        raise ValueError(f'Importe no interpretable: "{original}"')
    sign, integer, fraction = match.group(1), match.group(2), match.group(3) or ""
    result_negative = negative != bool(sign)
    return f"{'-' if result_negative else ''}{integer}{'.' + fraction if fraction else ''}"


def to_cents(value: object) -> int | None:
    """Convierte un importe a céntimos con redondeo mitad hacia arriba."""
    normalized = normalize_decimal_string(value)
    if normalized is None:
        return None
    match = _DECIMAL.match(normalized)
    assert match is not None
    sign, integer, fraction = match.group(1), match.group(2), match.group(3) or ""
    padded = fraction.ljust(3, "0")
    cents = int(integer) * 100 + int(padded[:2])
    if int(padded[2]) >= 5:
        cents += 1
    if cents > _MAX_CENTS:
        raise ValueError(f'Importe fuera de rango: "{value}"')
    return -cents if sign and cents != 0 else cents


def format_cents(cents: int | None) -> str:
    """11800 → "118.00"; None → ""."""
    if cents is None:
        return ""
    sign = "-" if cents < 0 else ""
    absolute = abs(int(cents))
    return f"{sign}{absolute // 100}.{absolute % 100:02d}"


def sum_cents(*values: int | None) -> int:
    """Suma céntimos ignorando los valores nulos."""
    return sum(v for v in values if v is not None)


def apply_rate_basis_points(cents: int, basis_points: int) -> int:
    """Multiplica céntimos por una tasa en puntos básicos (1800 = 18 %), mitad hacia arriba."""
    product = cents * basis_points
    quotient = abs(product) // 10000
    remainder = abs(product) % 10000
    if remainder * 2 >= 10000:
        quotient += 1
    return -quotient if product < 0 else quotient


def round_half_up_div(numerator: int, denominator: int) -> int:
    """numerator / denominator redondeado como Math.round (mitad hacia +∞), solo enteros."""
    if denominator <= 0:
        raise ValueError("El divisor debe ser positivo")
    return (2 * numerator + denominator) // (2 * denominator)


def js_round(value: float) -> int:
    """Math.round de JavaScript: la mitad redondea hacia +∞ (no al par, como round())."""
    return math.floor(value + 0.5)


def cents_to_decimal(cents: int | None) -> Decimal | None:
    """Céntimos → Decimal con dos decimales (para Excel y JSON)."""
    if cents is None:
        return None
    return (Decimal(int(cents)) / 100).quantize(Decimal("0.01"))
