from __future__ import annotations

from datetime import date

from invoicebot.domain.accounting import (
    Accounts,
    EntryInvoice,
    is_balanced,
    payment_date,
    provision_entry,
)


def invoice(**changes: object) -> EntryInvoice:
    base = {
        "invoice_id": 1,
        "issue_date": date(2026, 10, 15),
        "due_date": None,
        "supplier_ruc": "20100047218",
        "supplier_name": "PROVEEDOR",
        "document_type": "01",
        "series": "F001",
        "number": "1234",
        "currency": "PEN",
        "exchange_rate": None,
        "total_cents": 11_800,
        "igv_cents": 1_800,
        "other_taxes_cents": 0,
        "account_code": "6361",
        "cost_center": "ADM",
        "destination_account": None,
    }
    return EntryInvoice(**{**base, **changes})  # type: ignore[arg-type]


def summary(lines) -> list[tuple[str, int, int]]:
    return [(line.account, line.debit_cents, line.credit_cents) for line in lines]


def test_purchase_provision_with_destination():
    lines = provision_entry(invoice(destination_account="94"), Accounts())
    assert summary(lines) == [
        ("6361", 10_000, 0),
        ("40111", 1_800, 0),
        ("4212", 0, 11_800),
        ("94", 10_000, 0),
        ("791", 0, 10_000),
    ]
    assert is_balanced(lines) and lines[0].cost_center == "ADM"


def test_credit_note_reverses_and_default_expense():
    lines = provision_entry(
        invoice(document_type="07", account_code=None, total_cents=1_180, igv_cents=180), Accounts()
    )
    assert summary(lines) == [("6329", 0, 1_000), ("40111", 0, 180), ("4212", 1_180, 0)]
    assert is_balanced(lines)


def test_foreign_currency_converts_and_stays_balanced():
    lines = provision_entry(
        invoice(
            currency="USD",
            exchange_rate=3.757,
            total_cents=11_801,
            igv_cents=1_800,
            other_taxes_cents=1,
        ),
        Accounts(),
    )
    assert is_balanced(lines)
    assert lines[0].original_cents == 10_000 and lines[0].exchange_rate == 3.757
    assert lines[-1].account == "4212"


def test_payment_date_uses_due_date_or_terms():
    assert payment_date(date(2026, 10, 15), date(2026, 11, 30), 30) == date(2026, 11, 30)
    assert payment_date(date(2026, 10, 15), None, 30) == date(2026, 11, 14)
