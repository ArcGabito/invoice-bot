"""Fase 5: antifraude, portal de proveedores e indicadores (SQL Server real)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from invoicebot.api.app import create_app
from invoicebot.application import insights
from invoicebot.context import AppContext
from tests import builders
from tests.conftest import make_config

pytestmark = pytest.mark.db

SUPPLIER = "20100047218"
OTHER = "20131312955"


@pytest.fixture
def context(tmp_path: Path, db_env, clean_db) -> AppContext:
    ctx = AppContext(
        make_config(
            tmp_path,
            {
                "INVOICEBOT_PUBLIC_URL": "https://facturas.test",
                "INVOICEBOT_FRAUD_NEW_SUPPLIER_AMOUNT": "5000",
            },
            db_env=db_env,
        )
    )
    yield ctx
    ctx.close()


def _scan(ctx: AppContext, folder: Path, files: dict[str, bytes]) -> None:
    for name, content in files.items():
        builders.write(folder / name, content)
    ctx.jobs.enqueue("documents.scan", {"root": str(folder)})


def _events(ctx: AppContext, kind: str) -> list[dict]:
    with ctx.db.connect() as conn:
        return [
            json.loads(r[0])
            for r in conn.exec_driver_sql(
                "SELECT data FROM outbox_events WHERE type = ? ORDER BY id", (kind,)
            )
        ]


def test_fraud_signals_become_review_warnings(context: AppContext, tmp_path: Path):
    _scan(
        context,
        tmp_path / "entrada",
        {
            "grande.xml": builders.ubl_invoice(
                doc_id="F001-00000001", taxable="10000.00", igv="1800.00", total="11800.00"
            ),
            "gemela.xml": builders.ubl_invoice(
                doc_id="F002-00000077", taxable="10000.00", igv="1800.00", total="11800.00"
            ),
        },
    )
    with context.db.connect() as conn:
        rows = conn.exec_driver_sql(
            "SELECT i.series, v.rule_code, i.validation_status FROM validation_issues v "
            "JOIN invoices i ON i.id = v.invoice_id WHERE v.rule_code LIKE 'FRAUD[_]%'"
        ).all()
    # La primera que se procese es "proveedor nuevo con monto alto"; la otra, su gemela.
    by_series = {r[0]: r[1] for r in rows}
    assert set(by_series) == {"F001", "F002"}
    assert set(by_series.values()) == {"FRAUD_NEW_SUPPLIER_HIGH_AMOUNT", "FRAUD_FUZZY_DUPLICATE"}
    assert {r[2] for r in rows} == {"warning"}
    assert _events(context, "invoice.fraud_alert")


def test_supplier_portal_magic_link_status_and_upload(context: AppContext, tmp_path: Path):
    _scan(
        context,
        tmp_path / "entrada",
        {
            "mia.xml": builders.ubl_invoice(doc_id="F001-00000010"),
            "ajena.xml": builders.ubl_invoice(
                doc_id="F001-00000011", ruc=OTHER, name="OTRO S.A.C."
            ),
        },
    )
    insights.add_contact(context, supplier_ruc=SUPPLIER, email="Cobranzas@Proveedor.pe")
    with TestClient(create_app(context), base_url="https://invoicebot.test") as client:
        assert "Enviarme el enlace" in client.get("/portal").text
        assert client.get("/portal/api/invoices").status_code == 401

        # Correo desconocido: misma respuesta, sin enlace.
        res = client.post("/portal/api/login", json={"ruc": SUPPLIER, "email": "otro@gmail.com"})
        assert res.status_code == 200 and not _events(context, "portal.login_link")
        res = client.post(
            "/portal/api/login", json={"ruc": SUPPLIER, "email": "cobranzas@proveedor.pe"}
        )
        assert res.status_code == 200
        link = _events(context, "portal.login_link")[-1]["login_url"].replace(
            "https://facturas.test", ""
        )

        opened = client.get(link, follow_redirects=False)
        assert opened.status_code == 303 and opened.headers["location"] == "/portal"
        assert client.get(link, follow_redirects=False).headers["location"] == "/portal?expired=1"

        data = client.get("/portal/api/invoices").json()
        assert [i["invoice"] for i in data["items"]] == ["F001-00000010"]  # solo las suyas
        assert data["items"][0]["stage"] == "en revisión"

        files = [
            (
                "files",
                (
                    "F001-00000020.xml",
                    builders.ubl_invoice(doc_id="F001-00000020"),
                    "application/xml",
                ),
            )
        ]
        assert client.post("/portal/api/documents", files=files).status_code == 403  # sin cabecera
        res = client.post(
            "/portal/api/documents", files=files, headers={"x-requested-with": "portal"}
        )
        assert res.status_code == 202 and res.json()["files"] == 1
        bad = [("files", ("virus.exe", b"MZ", "application/octet-stream"))]
        assert (
            client.post(
                "/portal/api/documents", files=bad, headers={"x-requested-with": "portal"}
            ).status_code
            == 415
        )
        items = client.get("/portal/api/invoices").json()["items"]
        assert {i["invoice"] for i in items} == {"F001-00000010", "F001-00000020"}

        assert (
            client.post("/portal/api/logout", headers={"x-requested-with": "portal"}).status_code
            == 200
        )
        assert client.get("/portal/api/invoices").status_code == 401


def test_kpis(context: AppContext, tmp_path: Path):
    _scan(context, tmp_path / "entrada", {"a.xml": builders.ubl_invoice(doc_id="F001-00000030")})
    key, _ = context.users.create_api_key(name="bi", scope="read", created_by="t")
    month = __import__("datetime").date.today().strftime("%Y-%m")
    with TestClient(create_app(context), base_url="https://invoicebot.test") as client:
        res = client.get(
            f"/api/v1/analytics/kpis?month={month}", headers={"authorization": f"Bearer {key}"}
        )
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["received"] == 1 and data["from_xml"] == 1 and data["straight_through"] == 0
