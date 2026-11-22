"""Reglas tributarias de cuentas por pagar (Perú): SIRE, detracciones, retenciones y plazos.

Funciones puras, sin base de datos. Los importes van en céntimos.

> Los porcentajes, montos mínimos y formatos de archivo son los vigentes a la fecha de
> esta versión. SUNAT los cambia por resolución: revísalos antes de usarlos en producción.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date

# --- Claves de comprobante ------------------------------------------------------------


def document_key(
    supplier_ruc: str | None, document_type: str | None, series: str | None, number: str | None
) -> tuple[str, str, str, str]:
    """Clave para cruzar comprobantes: el número sin ceros a la izquierda ("00001234" = "1234")."""
    return (
        (supplier_ruc or "").strip(),
        (document_type or "").strip().zfill(2),
        (series or "").strip().upper(),
        (number or "").strip().lstrip("0") or "0",
    )


# --- SIRE: cruce con la propuesta del Registro de Compras --------------------------------


@dataclass(frozen=True)
class Voucher:
    """Un comprobante, venga de la propuesta de SUNAT o de InvoiceBot."""

    supplier_ruc: str | None
    document_type: str | None
    series: str | None
    number: str | None
    issue_date: date | None
    total_cents: int | None
    igv_cents: int | None
    currency: str | None = "PEN"
    ref: int | None = None

    @property
    def key(self) -> tuple[str, str, str, str]:
        return document_key(self.supplier_ruc, self.document_type, self.series, self.number)


@dataclass
class Difference:
    key: tuple[str, str, str, str]
    #: only_sunat | only_bot | amount | igv | date
    kind: str
    message: str
    sunat: Voucher | None = None
    bot: Voucher | None = None


@dataclass
class Reconciliation:
    matched: int = 0
    differences: list[Difference] = field(default_factory=list)

    def count(self, kind: str) -> int:
        return sum(1 for d in self.differences if d.kind == kind)

    def summary(self) -> dict[str, int]:
        kinds = ("only_sunat", "only_bot", "amount", "igv", "date")
        return {"matched": self.matched, **{k: self.count(k) for k in kinds}}


def reconcile_sire(
    proposal: Iterable[Voucher], registered: Iterable[Voucher], *, tolerance_cents: int = 1
) -> Reconciliation:
    """Cruza la propuesta del RCE de SUNAT con los comprobantes de InvoiceBot.

    - ``only_sunat``: SUNAT lo tiene y InvoiceBot no (pedir el comprobante al proveedor).
    - ``only_bot``: InvoiceBot lo tiene y la propuesta no (otro periodo, anulado o aún no
      informado por el proveedor: revisarlo antes de anotarlo).
    - ``amount`` / ``igv`` / ``date``: están en los dos, con datos distintos.
    """
    result = Reconciliation()
    sunat = {v.key: v for v in proposal}
    bot = {v.key: v for v in registered}
    for key, s in sunat.items():
        b = bot.get(key)
        if b is None:
            result.differences.append(
                Difference(key, "only_sunat", "Está en la propuesta de SUNAT y no en InvoiceBot", s)
            )
            continue
        found = False
        if (
            s.total_cents is not None
            and b.total_cents is not None
            and abs(s.total_cents - b.total_cents) > tolerance_cents
        ):
            result.differences.append(
                Difference(key, "amount", "El total no coincide con el de SUNAT", s, b)
            )
            found = True
        if (
            s.igv_cents is not None
            and b.igv_cents is not None
            and abs(s.igv_cents - b.igv_cents) > tolerance_cents
        ):
            result.differences.append(
                Difference(key, "igv", "El IGV no coincide con el de SUNAT", s, b)
            )
            found = True
        if s.issue_date and b.issue_date and s.issue_date != b.issue_date:
            result.differences.append(
                Difference(key, "date", "La fecha de emisión no coincide con la de SUNAT", s, b)
            )
            found = True
        if not found:
            result.matched += 1
    for key, b in bot.items():
        if key not in sunat:
            result.differences.append(
                Difference(
                    key, "only_bot", "Está en InvoiceBot y no en la propuesta de SUNAT", None, b
                )
            )
    return result


# --- Lectura de la propuesta (encabezados en lugar de posiciones) --------------------------


def _norm(value: str) -> str:
    plain = unicodedata.normalize("NFD", value)
    plain = "".join(c for c in plain if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", plain.lower()).strip()


#: Encabezado normalizado (contiene) → campo. El primero que coincide gana, en este orden.
PROPOSAL_COLUMNS: Sequence[tuple[str, str]] = (
    ("car sunat", "car"),
    ("fecha de emision", "issue_date"),
    ("fecha emision", "issue_date"),
    ("tipo cp", "document_type"),
    ("tipo de comprobante", "document_type"),
    ("serie", "series"),
    ("nro cp", "number"),
    ("numero de comprobante", "number"),
    ("nro doc identidad", "supplier_ruc"),
    ("numero de documento de identidad", "supplier_ruc"),
    ("ruc del proveedor", "supplier_ruc"),
    ("igv", "igv"),
    ("total cp", "total"),
    ("importe total", "total"),
    ("moneda", "currency"),
)


def map_proposal_header(header: Sequence[str]) -> dict[str, int]:
    """Columnas de la propuesta por su nombre. Lanza ValueError si falta una esencial."""
    mapping: dict[str, int] = {}
    for index, raw in enumerate(header):
        name = _norm(raw)
        for needle, field_name in PROPOSAL_COLUMNS:
            if field_name not in mapping and needle in name:
                # "IGV" aparece en varias columnas (DG, DGNG, DNG): vale la primera.
                mapping[field_name] = index
                break
    missing = {"document_type", "series", "number", "supplier_ruc", "total"} - set(mapping)
    if missing:
        raise ValueError(
            "La propuesta no tiene las columnas: "
            + ", ".join(sorted(missing))
            + " (¿es el archivo de exportación del RCE con encabezados?)"
        )
    return mapping


# --- Detracciones (SPOT) ----------------------------------------------------------------


def detraction_deposit_amount(amount_cents: int | None) -> int:
    """El depósito de detracción se hace en soles enteros, redondeado (sin decimales)."""
    if not amount_cents:
        return 0
    return int((amount_cents + 50) // 100) * 100


@dataclass(frozen=True)
class SpotPayment:
    supplier_ruc: str
    supplier_name: str
    account: str
    service_code: str
    amount_cents: int
    period: str  # AAAAMM
    document_type: str
    series: str
    number: str


def spot_payment_file(
    *, buyer_ruc: str, buyer_name: str, lot: str, payments: Sequence[SpotPayment]
) -> str:
    """Archivo de depósito masivo de detracciones (texto de ancho fijo).

    Cabecera: ``*`` + RUC del adquirente (11) + razón social (35) + número de lote (6,
    AANNNN) + importe total en soles enteros (15). Detalle por comprobante: tipo de
    documento del proveedor (``6`` = RUC) + RUC (11) + razón social (35) + proforma (9
    ceros) + código del bien o servicio (3) + cuenta del Banco de la Nación (11) +
    importe en soles enteros (15) + tipo de operación (2, ``01``) + periodo (6) + tipo de
    comprobante (2) + serie (4) + número (8).

    > Layout de la guía de depósitos masivos de SUNAT conocida a esta versión: verifícalo
    > con la guía vigente antes de subir el archivo.
    """
    if not re.fullmatch(r"\d{6}", lot):
        raise ValueError("El lote son 6 dígitos: año (2) y correlativo (4), p. ej. 260001")

    def text(value: str, size: int) -> str:
        plain = unicodedata.normalize("NFD", value or "")
        plain = "".join(c for c in plain if not unicodedata.combining(c)).upper()
        return re.sub(r"[^A-Z0-9 &.,-]", " ", plain)[:size].ljust(size)

    def amount(cents: int) -> str:
        return str(detraction_deposit_amount(cents) // 100).zfill(15)

    total = sum(detraction_deposit_amount(p.amount_cents) for p in payments)
    lines = [f"*{buyer_ruc}{text(buyer_name, 35)}{lot}{str(total // 100).zfill(15)}"]
    for p in payments:
        lines.append(
            "6"
            + p.supplier_ruc
            + text(p.supplier_name, 35)
            + "0" * 9
            + p.service_code.zfill(3)[:3]
            + re.sub(r"\D", "", p.account).zfill(11)[-11:]
            + amount(p.amount_cents)
            + "01"
            + p.period
            + p.document_type.zfill(2)
            + p.series.upper()[:4].rjust(4, "0")
            + p.number.lstrip("0").zfill(8)[-8:]
        )
    return "\r\n".join(lines) + "\r\n"


# --- Retenciones del IGV ------------------------------------------------------------------

RETENTION_RATE = 0.03
#: No se retiene si el importe de la operación no supera S/ 700.
RETENTION_THRESHOLD_CENTS = 70_000


@dataclass(frozen=True)
class RetentionCase:
    buyer_is_agent: bool
    supplier_is_agent: bool
    supplier_good_taxpayer: bool
    supplier_perception_agent: bool
    subject_to_detraction: bool
    document_type: str | None
    total_pen_cents: int


def retention_cents(case: RetentionCase) -> tuple[int, str]:
    """(importe a retener, motivo). 0 con el motivo cuando no corresponde."""
    if not case.buyer_is_agent:
        return 0, "la empresa no es agente de retención"
    if case.document_type not in ("01", "08"):
        return 0, "solo facturas y notas de débito"
    if case.subject_to_detraction:
        return 0, "la operación está sujeta a detracción"
    if case.supplier_is_agent or case.supplier_good_taxpayer or case.supplier_perception_agent:
        return 0, "el proveedor es agente o buen contribuyente"
    if case.total_pen_cents <= RETENTION_THRESHOLD_CENTS:
        return 0, "el importe no supera S/ 700"
    return round(case.total_pen_cents * RETENTION_RATE), "3 % del importe total"


# --- Plazos del crédito fiscal -----------------------------------------------------------


def annotation_deadline(issue_date: date) -> date:
    """Último día del periodo en que aún puede anotarse el comprobante para usar el crédito
    fiscal: el duodécimo periodo siguiente al de emisión."""
    month = issue_date.month - 1 + 12
    year = issue_date.year + month // 12
    month = month % 12 + 1
    following = date(year + (month == 12), month % 12 + 1, 1)
    return date.fromordinal(following.toordinal() - 1)
