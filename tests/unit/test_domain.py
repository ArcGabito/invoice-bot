from __future__ import annotations

import pytest

from invoicebot.domain.detraction import check_detraction, net_payable_cents, total_in_pen
from invoicebot.domain.duplicates import business_key
from invoicebot.domain.exchange_rate import ExchangeRate, to_pen_cents
from invoicebot.domain.invoice import (
    InvoiceData,
    has_invoice_data,
    normalize_date,
    normalize_invoice,
)
from invoicebot.domain.review import CorrectionError, parse_correction, split_assignment
from invoicebot.domain.ruc import compute_ruc_check_digit, normalize_ruc, validate_ruc
from invoicebot.domain.sunat_status import SunatResult, evaluate_sunat_result
from invoicebot.domain.validation import validate_invoice
from invoicebot.money import (
    apply_rate_basis_points,
    format_cents,
    normalize_decimal_string,
    to_cents,
)


def codes(issues):
    return {i.rule_code for i in issues}


class TestMoney:
    @pytest.mark.parametrize(
        ("raw", "cents"),
        [
            ("118.00", 11800),
            ("S/ 1,234.56", 123456),
            ("1.234,56", 123456),
            ("US$ 10", 1000),
            ("(15.00)", -1500),
            ("12,5", 1250),
            ("1,234", 123400),
            ("1.234.567", 123456700),
            ("0.005", 1),
            ("0.004", 0),
            (10.46, 1046),
            (0.1, 10),
            ("", None),
            (None, None),
        ],
    )
    def test_to_cents(self, raw, cents):
        assert to_cents(raw) == cents

    @pytest.mark.parametrize("raw", ["abc", "1.2.3,4,5", True, float("nan")])
    def test_rejects_garbage(self, raw):
        with pytest.raises(ValueError):
            to_cents(raw)

    def test_format_and_rates(self):
        assert format_cents(11800) == "118.00"
        assert format_cents(-5) == "-0.05"
        assert format_cents(None) == ""
        assert apply_rate_basis_points(10000, 1800) == 1800
        assert apply_rate_basis_points(333, 1800) == 60  # 59.94 → 60
        assert normalize_decimal_string("PEN 99") == "99"


class TestRuc:
    def test_check_digit(self):
        assert compute_ruc_check_digit("2010004721") == 8
        assert validate_ruc("20100047218").valid
        result = validate_ruc("20100047219")
        assert (result.valid, result.reason, result.expected) == (False, "check_digit", "8")
        assert validate_ruc("30100047218").reason == "prefix"
        assert validate_ruc("2010004721").reason == "format"
        assert normalize_ruc(" 20-100.047 218 ") == "20100047218"


class TestNormalization:
    @pytest.mark.parametrize(
        ("raw", "iso"),
        [
            ("2026-10-5", "2026-10-05"),
            ("15/10/2026", "2026-10-15"),
            ("August 5, 2025", "2025-08-05"),
            ("5 de agosto de 2025", "2025-08-05"),
            ("17 setiembre 2026", "2026-09-17"),
            ("mañana", "mañana"),
        ],
    )
    def test_dates(self, raw, iso):
        assert normalize_date(raw) == iso

    def test_invoice(self):
        result = normalize_invoice(
            {
                "supplier_ruc": "20100047218",
                "document_type": "Factura Electrónica",
                "series": "f001",
                "number": "0000 1234",
                "currency": "S/",
                "total": "S/ 118.00",
                "igv": "dieciocho",
                "supplier_country": "Estados Unidos",
                "detraction_code": "Código 37",
                "detraction_percent": "12 %",
            }
        )
        inv = result.invoice
        assert (inv.document_type, inv.series, inv.number, inv.currency) == (
            "01",
            "F001",
            "00001234",
            "PEN",
        )
        assert inv.total_cents == 11800 and inv.igv_cents is None
        assert inv.supplier_country == "US"
        assert (inv.detraction_code, inv.detraction_percent) == ("037", 12.0)
        assert [n.field for n in result.normalization_issues] == ["igv"]

    def test_has_invoice_data(self):
        assert has_invoice_data(
            {"document_type": "01", "number": "1", "supplier_name": "X", "total": "1"}
        )
        assert not has_invoice_data({"document_type": "01", "number": "1", "total": "1"})


def valid_invoice(**changes) -> InvoiceData:
    base = InvoiceData(
        supplier_name="PROVEEDOR",
        supplier_ruc="20100047218",
        customer_ruc="20601234565",
        document_type="01",
        series="F001",
        number="1234",
        issue_date="2026-10-15",
        currency="PEN",
        taxable_amount_cents=10000,
        igv_cents=1800,
        total_cents=11800,
    )
    return base.with_changes(**changes)


class TestValidation:
    def test_valid(self):
        status, issues = validate_invoice(valid_invoice(), today="2026-10-31")
        assert status == "valid", issues

    def test_errors(self):
        status, issues = validate_invoice(
            valid_invoice(supplier_ruc="20100047219", total_cents=12000, issue_date="2026-02-30"),
            today="2026-10-31",
        )
        assert status == "error"
        assert {"SUPPLIER_RUC_CHECK_DIGIT", "TOTAL_MISMATCH", "ISSUE_DATE_INVALID"} <= codes(issues)

    def test_warnings(self):
        status, issues = validate_invoice(
            valid_invoice(
                igv_cents=1000, total_cents=11000, series="B001", issue_date="2026-12-01"
            ),
            today="2026-10-31",
        )
        assert status == "warning"
        assert {"IGV_RATE_MISMATCH", "SERIES_TYPE_MISMATCH", "ISSUE_DATE_FUTURE"} <= codes(issues)

    def test_reduced_igv_and_discounts(self):
        # 10,5 % de restaurantes (Ley 31556) y descuento global restado después.
        _, issues = validate_invoice(
            valid_invoice(igv_cents=1050, total_cents=11050), today="2026-10-31"
        )
        assert "IGV_RATE_MISMATCH" not in codes(issues)
        _, issues = validate_invoice(
            valid_invoice(discount_amount_cents=1000, total_cents=10800), today="2026-10-31"
        )
        assert "TOTAL_MISMATCH" not in codes(issues)

    def test_prepaid(self):
        _, issues = validate_invoice(
            valid_invoice(prepaid_amount_cents=5900, total_cents=5900), today="2026-10-31"
        )
        assert "TOTAL_MISMATCH" not in codes(issues)

    def test_foreign_supplier(self):
        invoice = InvoiceData(
            supplier_name="Cloudflare, Inc.",
            supplier_country="US",
            document_type="91",
            number="IN-49955164",
            issue_date="2025-10-17",
            currency="USD",
            subtotal_cents=1046,
            total_cents=1046,
        )
        status, issues = validate_invoice(invoice, today="2026-10-31", exchange_rate=None)
        assert "REQUIRED_FIELD_MISSING" not in codes(issues)
        assert {"FOREIGN_SUPPLIER", "FX_RATE_MISSING"} <= codes(issues)
        assert status == "warning"

    def test_unparseable_amount_is_error(self):
        normalized = normalize_invoice({"total": "ciento dieciocho"})
        _, issues = validate_invoice(
            normalized.invoice,
            normalization_issues=normalized.normalization_issues,
            today="2026-10-31",
        )
        assert "AMOUNT_UNPARSEABLE" in codes(issues)


class TestDetraction:
    def test_rate_and_amount(self):
        invoice = valid_invoice(
            taxable_amount_cents=100000,
            igv_cents=18000,
            total_cents=118000,
            detraction_code="037",
            detraction_percent=12.0,
            detraction_amount_cents=14160,
        )
        assert check_detraction(invoice) == []
        wrong = invoice.with_changes(detraction_percent=10.0, detraction_amount_cents=5000)
        assert {"DETRACTION_RATE_MISMATCH", "DETRACTION_AMOUNT_MISMATCH"} <= codes(
            check_detraction(wrong)
        )

    def test_threshold_unknown_and_incomplete(self):
        small = valid_invoice(
            detraction_code="037", detraction_percent=12.0, detraction_amount_cents=1416
        )
        assert "DETRACTION_BELOW_THRESHOLD" in codes(check_detraction(small))
        assert "DETRACTION_UNKNOWN_CODE" in codes(
            check_detraction(valid_invoice(detraction_code="999"))
        )
        assert "DETRACTION_INCOMPLETE" in codes(
            check_detraction(valid_invoice(detraction_account="00-1"))
        )

    def test_dollars(self):
        invoice = valid_invoice(currency="USD", total_cents=118000, detraction_amount_cents=53100)
        assert total_in_pen(invoice, 3.75) == 442500
        assert to_pen_cents(1046, 3.7505) == 3924  # tipo de cambio redondeado a 3 decimales
        assert net_payable_cents(invoice, 3.75) == 118000 - 14160
        assert net_payable_cents(valid_invoice(detraction_amount_cents=1416), None) == 10384


class TestDuplicatesAndReview:
    def test_business_key(self):
        assert business_key(valid_invoice(number="00001234")) == "20100047218|01|F001|1234"
        foreign = InvoiceData(document_type="91", supplier_name="Cloudflare, Inc.", number="in-1")
        assert business_key(foreign) == "NOMBRE:CLOUDFLARE INC|91|IN-1"
        assert business_key(valid_invoice(series=None)) is None

    def test_corrections(self):
        assert parse_correction("total", "1,180.50").value == 118050
        assert parse_correction("issue_date", "15/10/2026").value == "2026-10-15"
        assert parse_correction("supplier_ruc", "").value is None
        assert parse_correction("detraction_percent", "12").display == "12"
        with pytest.raises(CorrectionError):
            parse_correction("validation_status", "valid")
        with pytest.raises(CorrectionError):
            parse_correction("total", "mucho")
        assert split_assignment("note=a=b") == ("note", "a=b")


class TestSunatStatus:
    def test_receipt_states(self):
        status, issues = evaluate_sunat_result(SunatResult("1", "00", "00"), document_type="01")
        assert status == "ACEPTADO" and issues == []
        status, issues = evaluate_sunat_result(SunatResult("0", "00", "00"), document_type="03")
        assert status == "NO EXISTE" and issues[0].severity == "warning"
        _, issues = evaluate_sunat_result(SunatResult("2", "01", "12", ["obs"]), document_type="01")
        assert codes(issues) == {
            "SUNAT_RECEIPT_VOIDED",
            "SUNAT_SUPPLIER_NOT_ACTIVE",
            "SUNAT_SUPPLIER_NOT_HABIDO",
            "SUNAT_OBSERVATION",
        }


def test_exchange_rate_dataclass():
    assert ExchangeRate(3.75, "2026-10-15").rate == 3.75
