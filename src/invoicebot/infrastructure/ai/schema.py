"""Contrato de extracción que debe cumplir cualquier proveedor de IA.

Los importes se piden como texto decimal ("1234.56") para no depender de la
coma flotante; la conversión a céntimos se hace en el dominio. Todos los campos
son obligatorios pero admiten null: el modelo debe decir explícitamente que no
encontró un dato en lugar de omitirlo o inventarlo.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

_AMOUNT = 'Texto decimal con punto y sin símbolo de moneda, p. ej. "1234.56". null si no aparece.'


def _amount(description: str) -> str:
    return f"{description}. {_AMOUNT}"


class Extraction(BaseModel):
    model_config = ConfigDict(extra="ignore")

    is_invoice: bool = Field(
        description="true si el documento es un comprobante de pago (factura, boleta, nota de "
        "crédito/débito, ticket, recibo)"
    )
    supplier_name: str | None = Field(description="Razón social o nombre del emisor")
    supplier_ruc: str | None = Field(
        description="RUC del emisor, 11 dígitos sin espacios. null si el emisor es del extranjero"
    )
    supplier_tax_id: str | None = Field(
        description="Identificador fiscal de un emisor del extranjero tal como aparece (EIN, VAT "
        "ID…). null si no aparece o si el emisor es peruano"
    )
    supplier_country: str | None = Field(
        description="País del emisor, código ISO de dos letras (PE, US, IE…)"
    )
    customer_name: str | None = Field(description="Razón social o nombre del cliente/adquiriente")
    customer_ruc: str | None = Field(description="RUC (11 dígitos) o DNI (8 dígitos) del cliente")
    document_type: str | None = Field(
        description="Código SUNAT del tipo de comprobante: 01 factura, 03 boleta, 07 nota de "
        "crédito, 08 nota de débito, 12 ticket, 14 recibo de servicios públicos, 91 factura de "
        "un proveedor del extranjero"
    )
    series: str | None = Field(description="Serie del comprobante, p. ej. F001, B001, E001 o 0001")
    number: str | None = Field(description="Número correlativo tal como aparece, p. ej. 00001234")
    issue_date: str | None = Field(description="Fecha de emisión en formato AAAA-MM-DD")
    due_date: str | None = Field(description="Fecha de vencimiento en formato AAAA-MM-DD")
    currency: str | None = Field(
        description="Código ISO 4217 de la moneda: PEN para soles, USD para dólares"
    )
    subtotal: str | None = Field(
        description=_amount("Subtotal o valor de venta antes de impuestos")
    )
    taxable_amount: str | None = Field(
        description=_amount("Operaciones gravadas (base imponible del IGV)")
    )
    exempt_amount: str | None = Field(description=_amount("Operaciones exoneradas"))
    unaffected_amount: str | None = Field(description=_amount("Operaciones inafectas"))
    discount_amount: str | None = Field(description=_amount("Total de descuentos globales"))
    igv: str | None = Field(description=_amount("IGV"))
    isc: str | None = Field(description=_amount("ISC"))
    icbper: str | None = Field(description=_amount("Impuesto a las bolsas plásticas (ICBPER)"))
    other_charges: str | None = Field(description=_amount("Otros cargos"))
    other_taxes: str | None = Field(description=_amount("Otros tributos"))
    total: str | None = Field(description=_amount("Importe total a pagar del comprobante"))
    prepaid_amount: str | None = Field(
        description=_amount("Anticipos ya pagados y deducidos del total")
    )
    detraction_code: str | None = Field(
        description="Detracción (SPOT): código de bien o servicio sujeto, 3 dígitos (p. ej. "
        '"037"). null si el comprobante no indica detracción'
    )
    detraction_percent: str | None = Field(
        description='Detracción: porcentaje tal como aparece, sin el signo % (p. ej. "12"). null '
        "si no indica detracción"
    )
    detraction_amount: str | None = Field(
        description=_amount("Detracción: monto a depositar, en soles")
    )
    detraction_account: str | None = Field(
        description="Detracción: número de cuenta del proveedor en el Banco de la Nación. null si "
        "no aparece"
    )
    notes: list[str] = Field(
        description="Observaciones breves sobre datos ilegibles, ambiguos o contradictorios. "
        "Lista vacía si no hay."
    )


EXTRACTION_FIELDS: tuple[str, ...] = tuple(Extraction.model_fields)


def empty_extraction() -> dict[str, object]:
    data: dict[str, object] = dict.fromkeys(EXTRACTION_FIELDS)
    data["is_invoice"] = False
    data["notes"] = []
    return data
