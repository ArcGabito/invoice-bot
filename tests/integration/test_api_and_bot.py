from __future__ import annotations

import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from invoicebot.api.app import create_app
from invoicebot.application.users import add_user, authenticate, reset_password, set_user_active
from invoicebot.context import AppContext
from invoicebot.errors import LockedError, UserError
from invoicebot.infrastructure.db.lock import run_lock
from tests import builders
from tests.conftest import make_config

pytestmark = pytest.mark.db

PASSWORD = "clave-de-prueba-123"


@pytest.fixture
def context(tmp_path: Path, db_env, clean_db) -> AppContext:
    config = make_config(tmp_path, {"INVOICEBOT_RUN_EXPORT": "xlsx"}, db_env=db_env)
    ctx = AppContext(config)
    yield ctx
    ctx.close()


@pytest.fixture
def client(context: AppContext):
    add_user(context.users, "ana", display_name="Ana Pérez", password=PASSWORD)
    with TestClient(create_app(context), base_url="https://invoicebot.test") as test_client:
        yield test_client


def login(client: TestClient) -> str:
    res = client.post("/api/v1/auth/login", json={"username": "Ana", "password": PASSWORD})
    assert res.status_code == 200, res.text
    return res.json()["csrf"]


def test_users_sessions_and_lockout(context: AppContext):
    created = add_user(context.users, "luis")
    assert created.generated and len(created.password) == 23
    with pytest.raises(UserError):
        add_user(context.users, "LUIS")
    assert authenticate(context.users, "luis", created.password)["username"] == "luis"
    user_id = created.user["id"]
    token, _ = context.users.create_session(user_id, hours=1, ip="1.2.3.4")
    assert context.users.get_session(token)["username"] == "luis"
    reset_password(context.users, "luis", password="otra-clave-larga-1")
    assert context.users.get_session(token) is None  # cambiar la contraseña cierra sesiones
    set_user_active(context.users, "luis", active=False)
    assert authenticate(context.users, "luis", "otra-clave-larga-1") is None


def test_login_csrf_and_review_ui(client: TestClient, context: AppContext, tmp_path: Path):
    assert client.get("/api/v1/queue").status_code == 401
    assert client.get("/", follow_redirects=False).status_code == 303
    for _ in range(5):
        client.post("/api/v1/auth/login", json={"username": "ana", "password": "mala-clave-xxx"})
    assert (
        client.post(
            "/api/v1/auth/login", json={"username": "ana", "password": PASSWORD}
        ).status_code
        == 429
    )
    client.app.state.throttle.clear(["ip:testclient", "user:ana"])

    csrf = login(client)
    page = client.get("/")
    assert page.status_code == 200 and "csrf-token" in page.text
    assert "default-src 'self'" in page.headers["content-security-policy"]
    assert page.headers["x-frame-options"] == "DENY"

    inbox = Path(context.config.run.inbox_dir)
    builders.write(inbox / "f.xml", builders.ubl_invoice(total="120.00"))
    from invoicebot.application.processing import scan_invoices

    scan_invoices(context.deps, root=str(inbox))
    queue = client.get("/api/v1/queue").json()
    assert queue["counts"]["pending"] == 1
    invoice_id = queue["items"][0]["invoice_id"]

    # Sin token antifalsificación no se acepta ninguna acción.
    assert client.post(f"/api/v1/invoices/{invoice_id}/approve", json={}).status_code == 403
    headers = {"x-csrf-token": csrf}
    res = client.post(
        f"/api/v1/invoices/{invoice_id}/correct",
        json={"changes": {"total": "118.00"}},
        headers=headers,
    )
    assert res.status_code == 200 and res.json()["validation_status"] == "valid"
    res = client.post(f"/api/v1/invoices/{invoice_id}/approve", json={}, headers=headers)
    assert res.json()["review_status"] == "approved"
    detail = client.get(f"/api/v1/invoices/{invoice_id}").json()
    assert detail["actions"][0]["user"] == "ana" and detail["invoice"]["created_at"].endswith("Z")

    document = client.get(f"/api/v1/invoices/{invoice_id}/document")
    assert document.status_code == 200 and document.headers["content-type"].startswith("text/plain")
    export = client.get("/api/v1/exports?format=xlsx&month=2026-10")
    assert (
        export.status_code == 200
        and export.headers["x-rows"] == "1"
        and export.content[:2] == b"PK"
    )
    assert client.post("/api/v1/auth/logout", headers=headers).status_code == 200
    assert client.get("/api/v1/queue").status_code == 401


def test_api_keys_upload_and_scopes(client: TestClient, context: AppContext):
    write_key, _ = context.users.create_api_key(name="email-bot", scope="write", created_by="ana")
    read_key, _ = context.users.create_api_key(name="power-bi", scope="read", created_by="ana")
    assert client.get("/api/v1/status", headers={"x-api-key": "ibk_falsa"}).status_code == 401

    files = [
        ("files", ("F001-00001234.xml", builders.ubl_invoice(), "application/xml")),
        (
            "files",
            ("F001-00001234.pdf", builders.invoice_pdf(number="00001234"), "application/pdf"),
        ),
    ]
    forbidden = client.post(
        "/api/v1/documents", files=files, headers={"authorization": f"Bearer {read_key}"}
    )
    assert forbidden.status_code == 403
    res = client.post(
        "/api/v1/documents", files=files, headers={"authorization": f"Bearer {write_key}"}
    )
    assert res.status_code == 201, res.text
    summary = res.json()["summary"]
    assert (summary["from_xml"], summary["linked_to_xml"]) == (1, 1)

    bad = client.post(
        "/api/v1/documents",
        files=[("files", ("virus.exe", b"MZ", "application/octet-stream"))],
        headers={"x-api-key": write_key},
    )
    assert bad.status_code == 415
    listing = client.get("/api/v1/invoices?month=2026-10", headers={"x-api-key": read_key}).json()
    assert listing["total"] == 1 and listing["items"][0]["supplier_ruc"] == "20100047218"
    assert (
        client.post(
            "/api/v1/fx",
            json={"date": "16/10/2026", "sell": 3.75},
            headers={"x-api-key": write_key},
        ).status_code
        == 200
    )
    assert (
        client.get("/api/v1/fx?month=2026-10", headers={"x-api-key": read_key}).json()["items"][0][
            "sell"
        ]
        == 3.75
    )
    assert (
        context.users.revoke_api_key(write_key.split("_")[0] + "_" + write_key.split("_")[1]) == 1
    )
    assert client.get("/api/v1/status", headers={"x-api-key": write_key}).status_code == 401
    assert client.get("/healthz").json()["status"] == "ok"
    assert client.get("/readyz").status_code == 200


def test_run_cycle_records_and_locks(context: AppContext):
    inbox = Path(context.config.run.inbox_dir)
    builders.write(inbox / "f.xml", builders.ubl_invoice())
    builders.write(inbox / "roto.pdf", b"%PDF roto")
    result = context.run_cycle(trigger="scheduled")
    assert result.status == "partial" and result.exit_code == 3
    compact = result.report.compact()
    assert compact["extracted"] == 1 and compact["failed"] == 1 and len(compact["exports"]) == 2
    runs = context.repository.list_bot_runs(1)
    assert runs[0]["status"] == "partial" and runs[0]["summary"]["found"] == 2

    acquired = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with run_lock(context.db):
            acquired.set()
            release.wait(10)

    holder = threading.Thread(target=hold)
    holder.start()
    acquired.wait(10)
    try:
        with pytest.raises(LockedError):
            context.run_cycle()
    finally:
        release.set()
        holder.join()
