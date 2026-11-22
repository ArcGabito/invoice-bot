"""Fase 4: asientos, programación de pagos, pagos de bank-bot y eventos para email-bot."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from invoicebot.api.app import create_app
from invoicebot.application import payables
from invoicebot.application.companies import add_company
from invoicebot.application.review import approve_invoice
from invoicebot.context import AppContext
from tests import builders
from tests.conftest import make_config

pytestmark = pytest.mark.db

COMPANY = "20601234565"


@pytest.fixture
def context(tmp_path: Path, db_env, clean_db) -> AppContext:
    ctx = AppContext(make_config(tmp_path, {"INVOICEBOT_PAYMENT_TERMS_DAYS": "30"}, db_env=db_env))
    yield ctx
    ctx.close()


def _events(ctx: AppContext) -> list[str]:
    with ctx.db.connect() as conn:
        return [r[0] for r in conn.exec_driver_sql("SELECT type FROM outbox_events ORDER BY id")]


def test_provision_schedule_payment_and_events(context: AppContext, tmp_path: Path):
    add_company(context.companies, ruc=COMPANY, name="Cliente", retention_agent=True)
    payables.add_cost_center(context, code="ADM", name="Administración", destination_account="94")
    inbox = tmp_path / "entrada"
    builders.write(
        inbox / "f.xml",
        builders.ubl_invoice(
            doc_id="F001-00000900", taxable="1000.00", igv="180.00", total="1180.00"
        ),
    )
    builders.write(inbox / "g.xml", builders.ubl_invoice(doc_id="F001-00000901"))
    context.jobs.enqueue("documents.scan", {"root": str(inbox)})
    with context.db.connect() as conn:
        ids = dict(conn.exec_driver_sql("SELECT number, id FROM invoices").all())
    big, small = ids["00000900"], ids["00000901"]
    assert _events(context).count("invoice.received") == 2

    from invoicebot.application.purchasing import set_accounting

    set_accounting(context, big, account_code="6361", cost_center="ADM")
    approve_invoice(context.review_deps(), big, user="ana", note="ok", force=True)
    context.after_review_approved(big)  # sin reglas: no requiere aprobación → se programa
    with context.db.connect() as conn:
        row = conn.exec_driver_sql(
            "SELECT payment_status, payable_cents, scheduled_payment_date FROM invoices WHERE id = ?",
            (big,),
        ).first()
    # 1 180.00 − retención 3 % (35.40) = 1 144.60, a 30 días de la emisión.
    assert (row[0], row[1], row[2]) == ("scheduled", 114_460, date(2026, 11, 14))
    assert "invoice.payment_scheduled" in _events(context)

    lines = payables.journal(context, month="2026-10")
    accounts = [(line.account, line.debit_cents, line.credit_cents) for line in lines]
    assert accounts == [
        ("6361", 100_000, 0),
        ("40111", 18_000, 0),
        ("4212", 0, 118_000),
        ("94", 100_000, 0),
        ("791", 0, 100_000),
    ]
    assert payables.journal_xlsx(lines, concar=True)[:2] == b"PK"

    treasury, _ = context.users.create_api_key(name="bank-bot", scope="write", created_by="t")
    reader, _ = context.users.create_api_key(name="bi", scope="read", created_by="t")
    with TestClient(create_app(context), base_url="https://invoicebot.test") as client:
        listed = client.get(
            "/api/v1/payables", headers={"authorization": f"Bearer {reader}"}
        ).json()
        assert [p["invoice_id"] for p in listed["items"]] == [big]
        body = {
            "supplier_ruc": "20100047218",
            "series": "F001",
            "number": "900",
            "amount": 600,
            "paid_on": "2026-11-14",
            "reference": "OP-1",
        }
        assert (
            client.post(
                "/api/v1/payments", json=body, headers={"authorization": f"Bearer {reader}"}
            ).status_code
            == 403
        )
        res = client.post(
            "/api/v1/payments", json=body, headers={"authorization": f"Bearer {treasury}"}
        )
        assert res.status_code == 201 and res.json()["payment_status"] == "partial", res.text
        again = client.post(
            "/api/v1/payments", json=body, headers={"authorization": f"Bearer {treasury}"}
        )
        assert again.json().get("duplicate") is True
        res = client.post(
            "/api/v1/payments",
            json={**body, "amount": 544.60, "reference": "OP-2"},
            headers={"authorization": f"Bearer {treasury}"},
        )
        assert res.json()["payment_status"] == "paid"
        csv_export = client.get(
            "/api/v1/accounting/entries?month=2026-10&format=csv",
            headers={"authorization": f"Bearer {reader}"},
        )
        assert csv_export.status_code == 200 and "4212" in csv_export.text

        # Rechazo en revisión: evento con el motivo para avisar al proveedor.
        res = client.post("/api/v1/auth/login", json={"username": "x", "password": "y"})
        assert res.status_code == 401
    from invoicebot.application.review import reject_invoice

    reject_invoice(context.review_deps(), small, user="ana", reason="RUC del adquirente equivocado")
    context.after_review_rejected(small, "RUC del adquirente equivocado")
    kinds = _events(context)
    assert kinds.count("invoice.paid") == 2 and "invoice.rejected" in kinds
