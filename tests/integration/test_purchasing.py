"""Fase 2: órdenes de compra, match a 3 vías, aprobación de pago, notas y webhooks."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect as sa_inspect

from invoicebot.api.app import create_app
from invoicebot.application import approvals, purchasing
from invoicebot.application.companies import add_company
from invoicebot.application.integrations import add_webhook
from invoicebot.application.users import add_user
from invoicebot.context import AppContext
from invoicebot.infrastructure.db.models import Base, utcnow
from tests import builders
from tests.conftest import make_config

pytestmark = pytest.mark.db

PASSWORD = "clave-de-prueba-123"
SUPPLIER = "20100047218"
COMPANY = "20601234565"


def with_refs(
    xml: bytes, *, order: str | None = None, despatch: str | None = None, billing: str | None = None
) -> bytes:
    refs = ""
    if order:
        refs += f"<cac:OrderReference><cbc:ID>{order}</cbc:ID></cac:OrderReference>"
    if despatch:
        refs += f"<cac:DespatchDocumentReference><cbc:ID>{despatch}</cbc:ID></cac:DespatchDocumentReference>"
    if billing:
        refs += (
            "<cac:BillingReference><cac:InvoiceDocumentReference>"
            f"<cbc:ID>{billing}</cbc:ID></cac:InvoiceDocumentReference></cac:BillingReference>"
        )
    text = xml.decode()
    marker = "</cbc:DocumentCurrencyCode>"
    return text.replace(marker, marker + refs, 1).encode()


class Receiver:
    """Receptor de webhooks: valida la firma como lo haría email-bot."""

    def __init__(self) -> None:
        self.secret = ""
        self.events: list[dict[str, Any]] = []
        self.fail_next = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.fail_next:
            self.fail_next -= 1
            return httpx.Response(503)
        body = request.content
        stamp = request.headers["x-invoicebot-timestamp"]
        expected = (
            "sha256="
            + hmac.new(
                self.secret.encode(), stamp.encode() + b"." + body, hashlib.sha256
            ).hexdigest()
        )
        assert hmac.compare_digest(request.headers["x-invoicebot-signature"], expected)
        self.events.append(json.loads(body))
        return httpx.Response(204)

    def types(self) -> list[str]:
        return [e["type"] for e in self.events]


@pytest.fixture
def receiver() -> Receiver:
    return Receiver()


@pytest.fixture
def context(tmp_path: Path, db_env, clean_db, receiver: Receiver) -> AppContext:
    config = make_config(
        tmp_path,
        {
            "INVOICEBOT_AUTO_APPROVE_MATCHED": "true",
            "INVOICEBOT_PUBLIC_URL": "https://facturas.test",
        },
        db_env=db_env,
    )
    ctx = AppContext(config)
    ctx.__dict__["http"] = httpx.Client(transport=httpx.MockTransport(receiver.handler))
    add_company(ctx.companies, ruc=COMPANY, name="Empresa A")
    hook = add_webhook(
        ctx, name="email-bot", url="https://email-bot.test/hooks", events="invoice.*,approval.*"
    )
    receiver.secret = hook["secret"]
    yield ctx
    ctx.close()


def _scan(ctx: AppContext, folder: Path, files: dict[str, bytes]) -> dict[str, int]:
    for name, content in files.items():
        builders.write(folder / name, content)
    result = ctx.jobs.enqueue("documents.scan", {"root": str(folder)})
    assert ctx.job_repository.get(result.id)["status"] == "succeeded"
    with ctx.db.connect() as conn:
        rows = conn.exec_driver_sql("SELECT series + '-' + number, id FROM invoices").all()
    return {r[0]: int(r[1]) for r in rows}


def _invoice(ctx: AppContext, invoice_id: int) -> dict[str, Any]:
    with ctx.db.connect() as conn:
        row = conn.exec_driver_sql(
            "SELECT review_status, validation_status, approval_status, account_code, cost_center, "
            "accounting_source, applied_to_invoice_id FROM invoices WHERE id = ?",
            (invoice_id,),
        ).first()
        issues = [
            r[0]
            for r in conn.exec_driver_sql(
                "SELECT rule_code FROM validation_issues WHERE invoice_id = ? AND resolved_at IS NULL",
                (invoice_id,),
            )
        ]
    keys = ("review", "validation", "approval", "account", "cost_center", "source", "applied_to")
    return {**dict(zip(keys, row, strict=True)), "issues": issues}


def test_orm_models_match_the_schema(context: AppContext):
    inspector = sa_inspect(context.db.engine)
    for table in Base.metadata.sorted_tables:
        columns = {c["name"] for c in inspector.get_columns(table.name)}
        missing = {c.name for c in table.columns} - columns
        assert not missing, f"{table.name}: faltan {missing}"


def test_three_way_match_auto_approval_and_webhooks(
    context: AppContext, tmp_path: Path, receiver: Receiver
):
    approvals.add_rule(
        context, level=1, approver=None, escalate_to="jefe", max_amount_cents=500_000
    )
    approvals.add_rule(context, level=2, approver="gerente", min_amount_cents=500_000)
    order = purchasing.upsert_purchase_order(
        context,
        {
            "po_number": "OC-2026-001",
            "supplier_ruc": SUPPLIER,
            "company_ruc": COMPANY,
            "cost_center": "ADM",
            "lines": [
                {"line_no": 1, "item_code": "PAP-A4", "quantity": "10", "unit_price": "10.00"}
            ],
        },
        source="api",
    )
    assert order["created"] and order["total_cents"] == 10_000

    ids = _scan(
        context,
        tmp_path / "entrada",
        {"f.xml": with_refs(builders.ubl_invoice(), order="OC-2026-001", despatch="T001-55")},
    )
    invoice_id = ids["F001-00001234"]
    match = purchasing.get_match(context, invoice_id)
    assert match is not None, [
        j["error_message"] for j in context.job_repository.list_jobs(task="purchasing.match")
    ]
    assert match["status"] == "exception"
    assert [e["code"] for e in match["exceptions"]] == ["PO_NOT_RECEIVED"]
    state = _invoice(context, invoice_id)
    assert state["review"] == "pending" and state["cost_center"] == "ADM", state

    # Llega la nota de ingreso: se rehace el match, coincide y la revisión se aprueba sola
    # (si la validación no tiene problemas); la regla automática autoriza el pago.
    purchasing.record_goods_receipt(
        context,
        {
            "receipt_number": "NI-778",
            "supplier_ruc": SUPPLIER,
            "company_ruc": COMPANY,
            "po_number": "OC-2026-001",
            "despatch_reference": "T001-55",
            "lines": [{"line_no": 1, "quantity": "10"}],
        },
        source="api",
    )
    result = purchasing.run_match(context, invoice_id)
    assert result["status"] == "matched" and result["found_by"] == "xml_order"
    state = _invoice(context, invoice_id)
    assert state["validation"] == "valid" and result["auto_approved_review"], state
    assert state["review"] == "approved" and state["approval"] == "approved", state
    assert {"invoice.match_exception", "invoice.matched", "invoice.approved_for_payment"} <= set(
        receiver.types()
    )
    envelope = receiver.events[0]
    assert envelope["specversion"] == "1.0" and envelope["source"] == "invoicebot"


def test_approval_levels_delegation_token_link_and_escalation(
    context: AppContext, tmp_path: Path, receiver: Receiver
):
    for user, role in (
        ("jefe", "approver"),
        ("asistente", "approver"),
        ("gerente", "approver"),
        ("ana", "accountant"),
    ):
        add_user(context.users, user, password=PASSWORD, role=role)
    approvals.add_rule(context, level=1, approver="jefe", escalate_to="gerente", sla_hours=24)
    approvals.add_rule(context, level=2, approver="gerente", min_amount_cents=500_000)
    today = utcnow().date()
    approvals.delegate(
        context,
        username="jefe",
        to="asistente",
        starts_on=today,
        ends_on=today + timedelta(days=3),
        reason="vacaciones",
    )
    big = builders.ubl_invoice(
        doc_id="F001-00009999", taxable="10000.00", igv="1800.00", total="11800.00"
    )
    small = builders.ubl_invoice(
        doc_id="F001-00000777", taxable="100.00", igv="18.00", total="118.00"
    )
    ids = _scan(context, tmp_path / "entrada", {"big.xml": big, "small.xml": small})
    big_id, small_id = ids["F001-00009999"], ids["F001-00000777"]

    with TestClient(create_app(context), base_url="https://invoicebot.test") as client:
        res = client.post("/api/v1/auth/login", json={"username": "ana", "password": PASSWORD})
        csrf = {"x-csrf-token": res.json()["csrf"]}
        for invoice_id in (big_id, small_id):
            res = client.post(
                f"/api/v1/invoices/{invoice_id}/approve",
                json={"force": True, "note": "ok"},
                headers=csrf,
            )
            assert res.status_code == 200, res.text
        client.post("/api/v1/auth/logout", headers=csrf)

        # Nivel 1 de la factura grande: jefe está de vacaciones → la recibe asistente.
        pending = approvals.list_requests(context, status="pending")
        first = next(p for p in pending if p["invoice_id"] == big_id)
        assert first["level"] == 1 and first["approver"] == "asistente"
        requested = [e for e in receiver.events if e["type"] == "approval.requested"]
        assert requested and requested[0]["data"]["approve_url"].startswith(
            "https://facturas.test/approve/"
        )

        res = client.post(
            "/api/v1/auth/login", json={"username": "asistente", "password": PASSWORD}
        )
        csrf = {"x-csrf-token": res.json()["csrf"]}
        res = client.post(
            f"/api/v1/approvals/{first['id']}/approve", json={"comment": "conforme"}, headers=csrf
        )
        assert res.status_code == 200 and res.json()["approval_status"] == "pending", res.text
        # La pequeña no pasa del nivel 1; asistente la rechaza (con motivo).
        small_request = next(p for p in pending if p["invoice_id"] == small_id)
        assert (
            client.post(
                f"/api/v1/approvals/{small_request['id']}/reject", json={}, headers=csrf
            ).status_code
            == 400
        )
        res = client.post(
            f"/api/v1/approvals/{small_request['id']}/reject",
            json={"comment": "precio fuera de lo pactado"},
            headers=csrf,
        )
        assert res.json()["approval_status"] == "rejected"
        client.post("/api/v1/auth/logout", headers=csrf)

        # Nivel 2 por el enlace de un clic (sin iniciar sesión).
        level2 = [
            e
            for e in receiver.events
            if e["type"] == "approval.requested" and e["data"]["level"] == 2
        ]
        url = level2[-1]["data"]["approve_url"].replace("https://facturas.test", "")
        page = client.get(url)
        assert page.status_code == 200 and "F001-00009999" in page.text
        done = client.post(url, data={"decision": "approve", "comment": ""})
        assert done.status_code == 200 and "autorizada para pago" in done.text
        assert client.get(url).status_code == 410  # un solo uso

    assert _invoice(context, big_id)["approval"] == "approved"
    assert _invoice(context, small_id)["approval"] == "rejected"
    assert {"invoice.approved_for_payment", "invoice.payment_rejected"} <= set(receiver.types())


def test_reminders_escalation_accounting_and_credit_notes(
    context: AppContext, tmp_path: Path, receiver: Receiver
):
    approvals.add_rule(context, level=1, approver="jefe", escalate_to="gerente", sla_hours=1)
    ids = _scan(context, tmp_path / "entrada", {"f.xml": builders.ubl_invoice()})
    invoice_id = ids["F001-00001234"]

    # Contabilidad asigna cuenta y centro de costo: se aprende para el proveedor.
    purchasing.set_accounting(context, invoice_id, account_code="6361", cost_center="ADM")
    later = _scan(
        context, tmp_path / "entrada2", {"g.xml": builders.ubl_invoice(doc_id="F001-00001300")}
    )
    learned = _invoice(context, later["F001-00001300"])
    assert (learned["account"], learned["cost_center"], learned["source"]) == (
        "6361",
        "ADM",
        "history",
    )

    from invoicebot.application.review import approve_invoice

    approve_invoice(context.review_deps(), invoice_id, user="ana", note="ok", force=True)
    assert approvals.start(context, invoice_id)["approval_status"] == "pending"
    now = utcnow()
    first = approvals.remind(context, now=now + timedelta(hours=2))
    assert first == {"reminded": 1, "escalated": 0}
    assert approvals.remind(context, now=now + timedelta(hours=3)) == {
        "reminded": 0,
        "escalated": 0,
    }
    second = approvals.remind(context, now=now + timedelta(hours=30))
    assert second == {"reminded": 0, "escalated": 1}
    pending = approvals.list_requests(context, status="pending")
    assert [p["approver"] for p in pending if p["invoice_id"] == invoice_id] == ["gerente"]
    assert {"approval.reminder", "approval.escalated"} <= set(receiver.types())

    # Nota de crédito por 18.00 aplicada a su factura: el saldo baja.
    note = with_refs(
        builders.ubl_invoice(
            root="CreditNote", doc_id="FC01-00000010", taxable="15.25", igv="2.75", total="18.00"
        ),
        billing="F001-00001234",
    )
    notes = _scan(context, tmp_path / "notas", {"nc.xml": note})
    assert _invoice(context, notes["FC01-00000010"])["applied_to"] == invoice_id
    with context.db.connect() as conn:
        balance = conn.exec_driver_sql(
            "SELECT credit_cents, balance_cents FROM v_invoice_balances WHERE invoice_id = ?",
            (invoice_id,),
        ).first()
    assert tuple(balance) == (1800, 10_000)
    assert "invoice.note_applied" in receiver.types()


def test_webhook_retries_then_delivers(context: AppContext, tmp_path: Path, receiver: Receiver):
    receiver.fail_next = 1
    purchasing.upsert_purchase_order(
        context,
        {
            "po_number": "OC-9",
            "supplier_ruc": SUPPLIER,
            "requires_receipt": False,
            "lines": [{"quantity": "1", "unit_price": "100.00"}],
        },
        source="api",
    )
    _scan(context, tmp_path / "entrada", {"f.xml": with_refs(builders.ubl_invoice(), order="OC-9")})
    with context.db.connect() as conn:
        statuses = [r[0] for r in conn.exec_driver_sql("SELECT status FROM webhook_deliveries")]
    assert "pending" in statuses  # la primera entrega falló (503) y espera su reintento
    from datetime import UTC, datetime

    context.jobs.relay(now=datetime.now(UTC) + timedelta(hours=2))
    with context.db.connect() as conn:
        rows = conn.exec_driver_sql("SELECT status, attempts FROM webhook_deliveries").all()
    assert {r[0] for r in rows} == {"delivered"}
    assert max(r[1] for r in rows) == 2
