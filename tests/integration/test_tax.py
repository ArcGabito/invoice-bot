"""Fase 3: SIRE, detracciones, retenciones y alertas de plazo (SQL Server real)."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from invoicebot.api.app import create_app
from invoicebot.application import tax
from invoicebot.application.companies import add_company
from invoicebot.application.review import approve_invoice
from invoicebot.context import AppContext
from tests import builders
from tests.conftest import make_config

pytestmark = pytest.mark.db

SUPPLIER = "20100047218"
COMPANY = "20601234565"
HEADER = (
    "RUC|Razón social|Periodo|CAR SUNAT|Fecha de emisión|Fecha Vcto/Pago|Tipo CP/Doc.|Serie del CDP|"
    "Año|Nro CP o Doc. Nro Inicial (Rango)|Nro Final (Rango)|Tipo Doc Identidad|Nro Doc Identidad|"
    "Razón social proveedor|BI Gravado DG|IGV / IPM DG|Total CP|Moneda"
)


def proposal_line(series: str, number: str, total: str, igv: str, day: str = "15/10/2026") -> str:
    return (
        f"{COMPANY}|CLIENTE|202610|{SUPPLIER}01{series}{number}|{day}||01|{series}||{number}||6|"
        f"{SUPPLIER}|PROVEEDOR|0|{igv}|{total}|PEN"
    )


@pytest.fixture
def context(tmp_path: Path, db_env, clean_db) -> AppContext:
    ctx = AppContext(make_config(tmp_path, db_env=db_env))
    yield ctx
    ctx.close()


def _scan(ctx: AppContext, folder: Path, files: dict[str, bytes]) -> dict[str, int]:
    for name, content in files.items():
        builders.write(folder / name, content)
    assert (
        ctx.job_repository.get(ctx.jobs.enqueue("documents.scan", {"root": str(folder)}).id)[
            "status"
        ]
        == "succeeded"
    )
    with ctx.db.connect() as conn:
        return {
            r[0]: int(r[1])
            for r in conn.exec_driver_sql("SELECT series + '-' + number, id FROM invoices")
        }


def test_sire_import_and_reconciliation(context: AppContext, tmp_path: Path):
    _scan(
        context,
        tmp_path / "entrada",
        {
            "a.xml": builders.ubl_invoice(doc_id="F001-00001234"),
            "b.xml": builders.ubl_invoice(doc_id="F001-00001300"),
            "c.xml": builders.ubl_invoice(doc_id="F001-00001400"),
        },
    )
    proposal = tmp_path / "propuesta.txt"
    proposal.write_text(
        "\n".join(
            [
                HEADER,
                proposal_line("F001", "1234", "118.00", "18.00"),
                proposal_line("F001", "1400", "120.00", "18.00"),
                proposal_line("F001", "9999", "50.00", "7.63"),
            ]
        ),
        encoding="utf-8",
    )
    result = tax.import_sire_proposal(
        context, proposal, period="2026-10", company_ruc=None, created_by="t"
    )
    assert result["rows"] == 3
    data = tax.reconcile(context, period="2026-10")
    assert data["summary"] == {
        "matched": 1,
        "only_sunat": 1,
        "only_bot": 1,
        "amount": 1,
        "igv": 0,
        "date": 0,
    }
    only_bot = next(d for d in data["differences"] if d["kind"] == "only_bot")
    assert only_bot["number"] == "1300" and only_bot["invoice_id"]

    write_key, _ = context.users.create_api_key(name="contabilidad", scope="write", created_by="t")
    headers = {"authorization": f"Bearer {write_key}"}
    with TestClient(create_app(context), base_url="https://invoicebot.test") as client:
        res = client.post(
            "/api/v1/sire/proposals",
            files={"file": ("propuesta.txt", proposal.read_bytes(), "text/plain")},
            data={"period": "2026-10"},
            headers=headers,
        )
        assert res.status_code == 201, res.text
        res = client.get("/api/v1/sire/reconciliation?period=2026-10", headers=headers)
        assert res.json()["summary"]["matched"] == 1
        xlsx = client.get("/api/v1/sire/reconciliation?period=2026-10&format=xlsx", headers=headers)
        assert xlsx.status_code == 200 and xlsx.content[:2] == b"PK"
        assert (
            client.get("/api/v1/sire/reconciliation?period=2026-09", headers=headers).status_code
            == 400
        )


def test_detractions_retentions_and_alerts(context: AppContext, tmp_path: Path):
    add_company(context.companies, ruc=COMPANY, name="Cliente Demo S.A.", retention_agent=True)
    old_day = (date.today() - timedelta(days=330)).isoformat()
    ids = _scan(
        context,
        tmp_path / "entrada",
        {
            "spot.xml": builders.ubl_invoice(
                doc_id="F001-00000500",
                taxable="1200.00",
                igv="216.00",
                total="1416.00",
                detraction={"code": "022", "percent": "12.00", "amount": "170.00"},
            ),
            "grande.xml": builders.ubl_invoice(
                doc_id="F001-00000600", taxable="1000.00", igv="180.00", total="1180.00"
            ),
            "vieja.xml": builders.ubl_invoice(doc_id="F001-00000700", issue_date=old_day),
        },
    )
    spot, big, old = ids["F001-00000500"], ids["F001-00000600"], ids["F001-00000700"]
    for invoice_id in (spot, big, old):
        approve_invoice(context.review_deps(), invoice_id, user="ana", note="ok", force=True)
        context.after_review_approved(invoice_id)  # como la API y la CLI
    with context.db.connect() as conn:
        statuses = dict(conn.exec_driver_sql("SELECT id, approval_status FROM invoices").all())
    assert set(statuses.values()) == {"not_required"}, statuses

    pending = tax.pending_detractions(context)
    assert [p["invoice_id"] for p in pending] == [spot]
    assert pending[0]["deposit_cents"] == 17_000
    content = tax.detraction_payment_file(
        context, invoice_ids=[spot], lot="260001", company_ruc=COMPANY
    )
    assert content.startswith("*" + COMPANY) and "022" in content

    # Retención: solo la factura grande (la de detracción no; la vieja no supera S/ 700).
    month = date.fromisoformat("2026-10-15").strftime("%Y-%m")
    report = {
        r["invoice_id"]: r for r in tax.retention_report(context, month=month, company_ruc=COMPANY)
    }
    assert report[big]["retention_cents"] == 3540 and report[spot]["retention_cents"] == 0
    tax.set_supplier_status(context, SUPPLIER, good_taxpayer=True)
    report = {
        r["invoice_id"]: r for r in tax.retention_report(context, month=month, company_ruc=COMPANY)
    }
    assert report[big]["retention_cents"] == 0

    alerts = tax.compliance_alerts(context)
    assert alerts == {"annotation": 1, "detraction": 1}
    with context.db.connect() as conn:
        types = [r[0] for r in conn.exec_driver_sql("SELECT type FROM outbox_events")]
    assert "tax.annotation_deadline" in types and "detraction.pending" in types

    tax.record_deposit(
        context,
        invoice_id=spot,
        constancia="123456789",
        deposit_date=date.today(),
        amount_cents=17_000,
        created_by="t",
    )
    assert tax.pending_detractions(context) == []
    with pytest.raises(Exception, match="ya está registrada"):
        tax.record_deposit(
            context,
            invoice_id=spot,
            constancia="123456789",
            deposit_date=date.today(),
            amount_cents=17_000,
            created_by="t",
        )
    assert old
