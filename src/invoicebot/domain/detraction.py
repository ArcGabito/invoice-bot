"""Sistema de detracciones del IGV (SPOT, R.S. 183-2004/SUNAT y modificatorias).

Códigos del catálogo 54 de SUNAT (Anexo IV) y porcentajes vigentes según la
cartilla y los apéndices publicados por SUNAT (orientacion.sunat.gob.pe,
consultados el 2026-10-31). Solo se incluyen los códigos confirmados en esas
fuentes: un código que no está aquí se registra, pero su porcentaje no se
valida. Si SUNAT cambia una tasa, actualiza esta tabla y
DETRACTION_RATES_VERSION.
"""

from __future__ import annotations

from dataclasses import dataclass

from invoicebot.domain.exchange_rate import to_pen_cents
from invoicebot.domain.invoice import InvoiceData, NewIssue, Severity
from invoicebot.money import format_cents, js_round, round_half_up_div

DETRACTION_RATES_VERSION = "spot/2026-10-31"


@dataclass(frozen=True)
class DetractionCode:
    description: str
    #: Porcentaje vigente en centésimas de punto (12 % = 1200).
    rate_bp: int
    #: Anexo de la R.S. 183-2004/SUNAT.
    annex: int


DETRACTION_CODES: dict[str, DetractionCode] = {
    # Anexo 2: bienes
    "004": DetractionCode("Recursos hidrobiológicos", 400, 2),
    "005": DetractionCode("Maíz amarillo duro", 400, 2),
    "007": DetractionCode("Caña de azúcar", 1000, 2),
    "008": DetractionCode("Madera", 400, 2),
    "009": DetractionCode("Arena y piedra", 1000, 2),
    "010": DetractionCode("Residuos, subproductos, desechos y similares", 1500, 2),
    "011": DetractionCode("Bienes gravados con el IGV por renuncia a la exoneración", 1000, 2),
    "014": DetractionCode("Carnes y despojos comestibles", 400, 2),
    "016": DetractionCode("Aceite de pescado", 1000, 2),
    "017": DetractionCode(
        "Harina, polvo y pellets de pescado y otros invertebrados acuáticos", 400, 2
    ),
    "023": DetractionCode("Leche", 400, 2),
    "031": DetractionCode("Oro gravado con el IGV", 1000, 2),
    "032": DetractionCode("Páprika y otros frutos de capsicum o pimienta", 1000, 2),
    "034": DetractionCode("Minerales metálicos no auríferos", 1000, 2),
    "035": DetractionCode("Bienes exonerados del IGV", 150, 2),
    "036": DetractionCode("Oro y demás minerales metálicos exonerados del IGV", 150, 2),
    "039": DetractionCode("Minerales no metálicos", 1000, 2),
    "041": DetractionCode("Plomo", 1500, 2),
    # Anexo 3: servicios
    "012": DetractionCode("Intermediación laboral y tercerización", 1200, 3),
    "019": DetractionCode("Arrendamiento de bienes", 1000, 3),
    "020": DetractionCode("Mantenimiento y reparación de bienes muebles", 1200, 3),
    "021": DetractionCode("Movimiento de carga", 1000, 3),
    "022": DetractionCode("Otros servicios empresariales", 1200, 3),
    "024": DetractionCode("Comisión mercantil", 1000, 3),
    "025": DetractionCode("Fabricación de bienes por encargo", 1000, 3),
    "026": DetractionCode("Servicio de transporte de personas", 1000, 3),
    "030": DetractionCode("Contratos de construcción", 400, 3),
    "037": DetractionCode("Demás servicios gravados con el IGV", 1200, 3),
}

#: Importe hasta el cual los servicios del Anexo 3 no están sujetos (S/ 700).
ANNEX3_THRESHOLD_CENTS = 70_000
#: El depósito se redondea en la práctica a soles enteros: se acepta S/ 1 de diferencia.
_AMOUNT_TOLERANCE_CENTS = 100


def _has_detraction(i: InvoiceData) -> bool:
    return any(
        v is not None
        for v in (
            i.detraction_code,
            i.detraction_percent,
            i.detraction_amount_cents,
            i.detraction_account,
        )
    )


def _format_percent(bp: int) -> str:
    text = f"{bp / 100:.2f}"
    return f"{text.removesuffix('.00')} %"


def check_detraction(invoice: InvoiceData, *, exchange_rate: float | None = None) -> list[NewIssue]:
    """Comprueba la detracción que declara el comprobante.

    No deduce si una operación debería estar sujeta (depende de qué se compró):
    solo valida lo declarado. `exchange_rate` convierte el total a soles en
    comprobantes en otra moneda; sin él no se comprueba el monto.
    """
    if not _has_detraction(invoice):
        return []
    issues: list[NewIssue] = []
    code = invoice.detraction_code
    percent = invoice.detraction_percent
    amount = invoice.detraction_amount_cents

    missing = [
        label
        for label, value in (
            ("código del bien o servicio", code),
            ("porcentaje", percent),
            ("monto", amount),
        )
        if value is None
    ]
    if missing:
        issues.append(
            NewIssue(
                "DETRACTION_INCOMPLETE",
                Severity.WARNING,
                f"El comprobante indica detracción pero falta: {', '.join(missing)}",
            )
        )

    known = DETRACTION_CODES.get(code) if code else None
    if code and not known:
        issues.append(
            NewIssue(
                "DETRACTION_UNKNOWN_CODE",
                Severity.INFO,
                f"Código de detracción {code} sin tasa registrada en InvoiceBot: confirma el "
                "porcentaje con tu contador",
                None,
                code,
            )
        )
    percent_bp = None if percent is None else js_round(percent * 100)
    if known and percent_bp is not None and percent_bp != known.rate_bp:
        issues.append(
            NewIssue(
                "DETRACTION_RATE_MISMATCH",
                Severity.WARNING,
                "El porcentaje de detracción no coincide con la tasa de SUNAT para "
                f"{code} ({known.description})",
                _format_percent(known.rate_bp),
                _format_percent(percent_bp),
            )
        )

    total_pen = total_in_pen(invoice, exchange_rate)
    if amount is not None and percent_bp is not None and total_pen is not None:
        expected = round_half_up_div(total_pen * percent_bp, 10_000)
        if abs(expected - amount) > _AMOUNT_TOLERANCE_CENTS:
            issues.append(
                NewIssue(
                    "DETRACTION_AMOUNT_MISMATCH",
                    Severity.WARNING,
                    "El monto de detracción no corresponde al "
                    f"{_format_percent(percent_bp)} del total en soles",
                    format_cents(expected),
                    format_cents(amount),
                )
            )
    if known and known.annex == 3 and total_pen is not None and total_pen <= ANNEX3_THRESHOLD_CENTS:
        issues.append(
            NewIssue(
                "DETRACTION_BELOW_THRESHOLD",
                Severity.INFO,
                "El total no supera S/ 700: los servicios del Anexo 3 no están sujetos a "
                "detracción por ese importe",
                "> 700.00",
                format_cents(total_pen),
            )
        )
    return issues


def total_in_pen(invoice: InvoiceData, exchange_rate: float | None) -> int | None:
    """Total del comprobante en soles; None si falta el total o el tipo de cambio."""
    if invoice.total_cents is None:
        return None
    if not invoice.currency or invoice.currency == "PEN":
        return invoice.total_cents
    return None if exchange_rate is None else to_pen_cents(invoice.total_cents, exchange_rate)


def net_payable_cents(invoice: InvoiceData, exchange_rate: float | None) -> int | None:
    """Neto a pagar al proveedor en la moneda del comprobante: total menos detracción."""
    if invoice.total_cents is None:
        return None
    detraction = invoice.detraction_amount_cents
    if detraction is None:
        return invoice.total_cents
    if not invoice.currency or invoice.currency == "PEN":
        return invoice.total_cents - detraction
    if exchange_rate is None or exchange_rate <= 0:
        return None
    return invoice.total_cents - js_round(detraction / exchange_rate)
