from __future__ import annotations

from datetime import date

from invoicebot.domain.purchasing import (
    Delegation,
    InvoiceFacts,
    MatchTolerance,
    OrderFacts,
    Rule,
    approval_steps,
    effective_approver,
    match_invoice,
)

SUPPLIER = "20100047218"


def order(**changes: object) -> OrderFacts:
    base = {
        "po_number": "OC-1",
        "supplier_ruc": SUPPLIER,
        "currency": "PEN",
        "status": "open",
        "requires_receipt": True,
        "ordered_cents": 100_000,
        "received_cents": 100_000,
        "invoiced_cents": 0,
    }
    return OrderFacts(**{**base, **changes})  # type: ignore[arg-type]


def invoice(net: int = 100_000, **changes: object) -> InvoiceFacts:
    return InvoiceFacts(
        supplier_ruc=changes.get("supplier_ruc", SUPPLIER),  # type: ignore[arg-type]
        currency=changes.get("currency", "PEN"),  # type: ignore[arg-type]
        net_cents=net,
    )


TOLERANCE = MatchTolerance(percent=1.0, cents=1000)


def codes(outcome) -> set[str]:
    return {e.code for e in outcome.exceptions}


def test_three_way_match_and_tolerance():
    assert match_invoice(invoice(), order(), TOLERANCE).status == "matched"
    # 1 % de 1 000.00 = 10.00: 1 009.99 entra, 1 010.01 no.
    assert match_invoice(invoice(100_999), order(), TOLERANCE).status == "matched"
    over = match_invoice(invoice(101_001), order(), TOLERANCE)
    assert codes(over) == {"PO_AMOUNT_EXCEEDED", "PO_RECEIPT_EXCEEDED"}


def test_three_way_requires_receipt_and_respects_partial_receipts():
    assert codes(match_invoice(invoice(), order(received_cents=0), TOLERANCE)) == {
        "PO_NOT_RECEIVED"
    }
    partial = match_invoice(invoice(60_000), order(received_cents=50_000), TOLERANCE)
    assert codes(partial) == {"PO_RECEIPT_EXCEEDED"}
    # Ya facturado 60 000: queda 40 000 por facturar.
    second = match_invoice(invoice(50_000), order(invoiced_cents=60_000), TOLERANCE)
    assert "PO_AMOUNT_EXCEEDED" in codes(second)


def test_two_way_match_ignores_receipts():
    outcome = match_invoice(invoice(), order(requires_receipt=False, received_cents=0), TOLERANCE)
    assert outcome.status == "matched" and outcome.match_type == "two_way"


def test_supplier_currency_and_status_exceptions():
    wrong = match_invoice(
        invoice(supplier_ruc="20131312955", currency="USD"), order(status="closed"), TOLERANCE
    )
    assert codes(wrong) == {"PO_SUPPLIER_MISMATCH", "PO_CURRENCY_MISMATCH", "PO_NOT_OPEN"}
    assert all(e.message for e in wrong.exceptions)


def test_approval_matrix_levels_and_specificity():
    rules = [
        Rule(1, level=1, approver="jefe", max_cents=500_000),
        Rule(2, level=1, approver="jefe-ti", cost_center="TI", max_cents=500_000),
        Rule(3, level=1, approver="jefe", min_cents=500_000),
        Rule(4, level=2, approver="gerente", min_cents=500_000),
        Rule(5, level=2, approver="directorio", min_cents=5_000_000),
        Rule(6, level=3, approver="directorio", min_cents=5_000_000, company_id=1),
    ]

    def steps(amount: int, **kw: object) -> list[tuple[int, str | None]]:
        result = approval_steps(
            rules,
            company_id=kw.get("company_id"),  # type: ignore[arg-type]
            cost_center=kw.get("cost_center"),  # type: ignore[arg-type]
            supplier_ruc=None,
            amount_pen_cents=amount,
        )
        return [(s.level, s.approver) for s in result]

    assert steps(100_000) == [(1, "jefe")]
    assert steps(100_000, cost_center="TI") == [(1, "jefe-ti")]  # la más específica gana
    assert steps(500_000) == [(1, "jefe"), (2, "gerente")]
    assert steps(6_000_000) == [(1, "jefe"), (2, "gerente")]  # misma especificidad: la más antigua
    assert steps(6_000_000, company_id=1) == [(1, "jefe"), (2, "gerente"), (3, "directorio")]
    assert (
        approval_steps([], company_id=None, cost_center=None, supplier_ruc=None, amount_pen_cents=1)
        == []
    )


def test_delegations_follow_chains_and_stop_cycles():
    today = date(2026, 12, 10)
    delegations = [
        Delegation("jefe", "asistente", date(2026, 12, 1), date(2026, 12, 15)),
        Delegation("asistente", "suplente", date(2026, 12, 5), date(2026, 12, 20)),
        Delegation("gerente", "jefe", date(2026, 12, 1), date(2026, 12, 31)),
        Delegation("a", "b", date(2026, 1, 1), date(2026, 12, 31)),
        Delegation("b", "a", date(2026, 1, 1), date(2026, 12, 31)),
    ]
    assert effective_approver("jefe", delegations, today) == "suplente"
    assert effective_approver("jefe", delegations, date(2026, 12, 2)) == "asistente"
    assert effective_approver("jefe", delegations, date(2026, 12, 16)) == "jefe"
    assert effective_approver("a", delegations, today) == "b"  # ciclo: se detiene
