"""Asiento de provisión de compras según el Plan Contable General Empresarial (PCGE).

Para una factura de compra autorizada:

    Debe  60xx/63xx  gasto o compra (cuenta del proveedor o por defecto)   base
    Debe  40111      IGV - cuenta propia                                  IGV
    Debe  6xxx       otros tributos no recuperables (ISC, ICBPER…)        otros
       Haber 4212    facturas por pagar                                   total

Y, si el centro de costo tiene cuenta de destino (gasto por naturaleza):

    Debe  9x         destino del gasto (p. ej. 94 administración, 95 ventas)  base
       Haber 79      cargas imputables a cuentas de costos y gastos          base

Las notas de crédito (07) invierten debe y haber. En moneda extranjera se registra el
importe en soles con el tipo de cambio de la factura y el original en dólares.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Accounts:
    payable: str = "4212"
    igv: str = "40111"
    expense: str = "6329"
    other_taxes: str = "6419"
    destination_credit: str = "791"


@dataclass(frozen=True)
class EntryInvoice:
    invoice_id: int
    issue_date: date
    due_date: date | None
    supplier_ruc: str
    supplier_name: str
    document_type: str
    series: str
    number: str
    currency: str
    exchange_rate: float | None
    total_cents: int
    igv_cents: int
    other_taxes_cents: int
    account_code: str | None
    cost_center: str | None
    destination_account: str | None


@dataclass(frozen=True)
class JournalLine:
    invoice_id: int
    entry_date: date
    account: str
    debit_cents: int
    credit_cents: int
    currency: str
    #: Importe en la moneda del comprobante (solo si no es PEN).
    original_cents: int | None
    exchange_rate: float | None
    supplier_ruc: str
    document_type: str
    series: str
    number: str
    due_date: date | None
    cost_center: str | None
    description: str


def _pen(cents: int, invoice: EntryInvoice) -> int:
    if invoice.currency == "PEN" or not invoice.exchange_rate:
        return cents
    return round(cents * invoice.exchange_rate)


def provision_entry(invoice: EntryInvoice, accounts: Accounts) -> list[JournalLine]:
    """Líneas del asiento (debe = haber en soles, ajustando el redondeo en la cuenta por pagar)."""
    credit_note = invoice.document_type == "07"
    base = invoice.total_cents - invoice.igv_cents - invoice.other_taxes_cents
    description = f"{invoice.supplier_name[:40]} {invoice.series}-{invoice.number}".strip()

    def line(
        account: str, cents: int, *, debit: bool, cost_center: str | None = None
    ) -> JournalLine:
        debit_side = debit != credit_note  # una nota de crédito invierte el asiento
        pen = _pen(cents, invoice)
        return JournalLine(
            invoice_id=invoice.invoice_id,
            entry_date=invoice.issue_date,
            account=account,
            debit_cents=pen if debit_side else 0,
            credit_cents=0 if debit_side else pen,
            currency=invoice.currency,
            original_cents=cents if invoice.currency != "PEN" else None,
            exchange_rate=invoice.exchange_rate if invoice.currency != "PEN" else None,
            supplier_ruc=invoice.supplier_ruc,
            document_type=invoice.document_type,
            series=invoice.series,
            number=invoice.number,
            due_date=invoice.due_date,
            cost_center=cost_center,
            description=description,
        )

    lines = [
        line(
            invoice.account_code or accounts.expense,
            base,
            debit=True,
            cost_center=invoice.cost_center,
        )
    ]
    if invoice.igv_cents:
        lines.append(line(accounts.igv, invoice.igv_cents, debit=True))
    if invoice.other_taxes_cents:
        lines.append(line(accounts.other_taxes, invoice.other_taxes_cents, debit=True))
    payable = line(accounts.payable, invoice.total_cents, debit=False)
    # Redondeo de la conversión a soles: la diferencia va a la cuenta por pagar.
    debit = sum(item.debit_cents for item in lines)
    credit = sum(item.credit_cents for item in lines)
    gap = (debit - credit) - (payable.credit_cents - payable.debit_cents)
    if gap:
        payable = JournalLine(
            **{
                **payable.__dict__,
                "credit_cents": payable.credit_cents + gap if payable.credit_cents else 0,
                "debit_cents": payable.debit_cents - gap if payable.debit_cents else 0,
            }
        )
    lines.append(payable)
    if invoice.destination_account:
        lines.append(
            line(invoice.destination_account, base, debit=True, cost_center=invoice.cost_center)
        )
        lines.append(line(accounts.destination_credit, base, debit=False))
    return lines


def is_balanced(lines: list[JournalLine]) -> bool:
    return sum(item.debit_cents for item in lines) == sum(item.credit_cents for item in lines)


def payment_date(issue: date, due: date | None, default_terms_days: int) -> date:
    """Fecha programada de pago: el vencimiento del comprobante o la condición por defecto."""
    from datetime import timedelta

    return due or issue + timedelta(days=default_terms_days)
