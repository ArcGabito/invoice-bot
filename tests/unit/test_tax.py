from __future__ import annotations

from datetime import date

import pytest

from invoicebot.domain.tax import (
    RetentionCase,
    SpotPayment,
    Voucher,
    annotation_deadline,
    detraction_deposit_amount,
    document_key,
    map_proposal_header,
    reconcile_sire,
    retention_cents,
    spot_payment_file,
)

RUC = "20100047218"


def v(
    number: str, total: int = 11800, igv: int = 1800, day: date = date(2026, 10, 15), **kw: object
) -> Voucher:
    return Voucher(RUC, kw.get("type", "01"), kw.get("series", "F001"), number, day, total, igv)  # type: ignore[arg-type]


def test_document_key_ignores_leading_zeros_and_case():
    assert document_key(RUC, "1", "f001", "00001234") == document_key(RUC, "01", "F001", "1234")


def test_reconcile_sire_categories():
    sunat = [v("00000001"), v("2", total=10000), v("3", day=date(2026, 10, 16)), v("4")]
    bot = [v("1"), v("2"), v("3"), v("5")]
    result = reconcile_sire(sunat, bot)
    assert result.summary() == {
        "matched": 1,
        "only_sunat": 1,
        "only_bot": 1,
        "amount": 1,
        "igv": 0,
        "date": 1,
    }


def test_proposal_header_by_name():
    header = [
        "RUC",
        "Apellidos y Nombres o Razón social",
        "Periodo",
        "CAR SUNAT",
        "Fecha de emisión",
        "Fecha Vcto/Pago",
        "Tipo CP/Doc.",
        "Serie del CDP",
        "Año",
        "Nro CP o Doc. Nro Inicial (Rango)",
        "Nro Final (Rango)",
        "Tipo Doc Identidad",
        "Nro Doc Identidad",
        "Apellidos Nombres/ Razón Social",
        "BI Gravado DG",
        "IGV / IPM DG",
        "BI Gravado DGNG",
        "IGV / IPM DGNG",
        "Total CP",
        "Moneda",
    ]
    columns = map_proposal_header(header)
    assert columns["supplier_ruc"] == 12 and columns["igv"] == 15 and columns["total"] == 18
    assert columns["number"] == 9 and columns["series"] == 7 and columns["car"] == 3
    with pytest.raises(ValueError, match="columnas"):
        map_proposal_header(["RUC", "Fecha"])


def test_detraction_rounding_and_spot_file():
    assert detraction_deposit_amount(14_049) == 14_000
    assert detraction_deposit_amount(14_050) == 14_100
    content = spot_payment_file(
        buyer_ruc="20601234565",
        buyer_name="Cliente Demo S.A.",
        lot="260001",
        payments=[
            SpotPayment(
                RUC,
                "Proveedor Ñandú S.A.C.",
                "00-000-123456",
                "22",
                14_160,
                "202610",
                "01",
                "F001",
                "00001234",
            )
        ],
    )
    header, detail = content.splitlines()
    assert header == "*20601234565" + "CLIENTE DEMO S.A.".ljust(35) + "260001" + "142".zfill(15)
    assert len(detail) == 1 + 11 + 35 + 9 + 3 + 11 + 15 + 2 + 6 + 2 + 4 + 8
    assert detail.startswith("6" + RUC + "PROVEEDOR NANDU S.A.C.")
    assert detail.endswith("000000000000142" + "01" + "202610" + "01" + "F001" + "00001234")
    assert content.endswith("\r\n")
    with pytest.raises(ValueError):
        spot_payment_file(buyer_ruc="1", buyer_name="x", lot="26", payments=[])


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, 3540),
        ({"buyer_is_agent": False}, 0),
        ({"supplier_good_taxpayer": True}, 0),
        ({"subject_to_detraction": True}, 0),
        ({"document_type": "03"}, 0),
        ({"total_pen_cents": 70_000}, 0),
    ],
)
def test_retention(changes: dict[str, object], expected: int):
    base = {
        "buyer_is_agent": True,
        "supplier_is_agent": False,
        "supplier_good_taxpayer": False,
        "supplier_perception_agent": False,
        "subject_to_detraction": False,
        "document_type": "01",
        "total_pen_cents": 118_000,
    }
    amount, reason = retention_cents(RetentionCase(**{**base, **changes}))  # type: ignore[arg-type]
    assert amount == expected and reason


def test_annotation_deadline_is_twelve_periods_later():
    assert annotation_deadline(date(2026, 1, 15)) == date(2027, 1, 31)
    assert annotation_deadline(date(2026, 12, 3)) == date(2027, 12, 31)
    assert annotation_deadline(date(2026, 2, 28)) == date(2027, 2, 28)
