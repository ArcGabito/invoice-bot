from __future__ import annotations

from datetime import date

from invoicebot.domain.fraud import PastInvoice, detect

DAY = date(2026, 10, 15)


def codes(**kw: object) -> set[str]:
    base = {
        "series": "F001",
        "number": "100",
        "issue_date": DAY,
        "total_pen_cents": 50_000,
        "history": [],
    }
    return {s.code for s in detect(**{**base, **kw})}  # type: ignore[arg-type]


def test_new_supplier_with_high_amount():
    assert codes(total_pen_cents=1_500_000) == {"FRAUD_NEW_SUPPLIER_HIGH_AMOUNT"}
    assert codes(total_pen_cents=50_000) == set()


def test_amount_out_of_historical_range():
    history = [
        PastInvoice(i, "F001", str(i), date(2026, 9, i), 100_000 + i * 1_000) for i in range(1, 7)
    ]
    assert codes(total_pen_cents=110_000, history=history) == set()
    assert codes(total_pen_cents=500_000, history=history) == {"FRAUD_AMOUNT_OUT_OF_RANGE"}
    # Con poco historial no se juzga el rango.
    assert codes(total_pen_cents=500_000, history=history[:3]) == set()


def test_fuzzy_duplicate_same_amount_and_date_other_number():
    history = [PastInvoice(7, "F002", "00000099", DAY, 50_000)]
    assert codes(history=history) == {"FRAUD_FUZZY_DUPLICATE"}
    same = [PastInvoice(7, "F001", "00000100", DAY, 50_000)]
    assert codes(history=same) == set()  # el mismo comprobante lo detecta POSSIBLE_DUPLICATE
