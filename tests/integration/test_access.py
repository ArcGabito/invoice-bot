"""Fase 1: multiempresa (Row-Level Security), roles, auditoría y SSO (OIDC)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from invoicebot.api.app import create_app
from invoicebot.application.companies import add_company
from invoicebot.application.users import add_user
from invoicebot.context import AppContext
from invoicebot.infrastructure.oidc import OidcClient
from tests import builders
from tests.conftest import make_config

pytestmark = pytest.mark.db

PASSWORD = "clave-de-prueba-123"
COMPANY_A = "20601234565"  # el adquirente por defecto de builders.ubl_invoice
COMPANY_B = "20100070970"
OTHER = "20131312955"
ISSUER = "https://login.example.test/tenant/v2.0"


@pytest.fixture
def context(tmp_path: Path, db_env, clean_db) -> AppContext:
    ctx = AppContext(make_config(tmp_path, db_env=db_env))
    yield ctx
    ctx.close()


def _scan(ctx: AppContext, tmp_path: Path) -> None:
    inbox = tmp_path / "entrada"
    builders.write(inbox / "a.xml", builders.ubl_invoice(doc_id="F001-00000001"))
    builders.write(
        inbox / "b.xml", builders.ubl_invoice(doc_id="F001-00000002", customer_ruc=COMPANY_B)
    )
    builders.write(
        inbox / "c.xml", builders.ubl_invoice(doc_id="F001-00000003", customer_ruc=OTHER)
    )
    result = ctx.jobs.enqueue("documents.scan", {"root": str(inbox)})
    assert ctx.job_repository.get(result.id)["status"] == "succeeded"


def _login(client: TestClient, username: str) -> dict[str, str]:
    res = client.post("/api/v1/auth/login", json={"username": username, "password": PASSWORD})
    assert res.status_code == 200, res.text
    return {"x-csrf-token": res.json()["csrf"]}


def _invoices(client: TestClient, headers: dict[str, str] | None = None) -> dict[str, int]:
    res = client.get("/api/v1/invoices", headers=headers or {})
    assert res.status_code == 200, res.text
    return {i["series"] + "-" + i["number"]: i["invoice_id"] for i in res.json()["items"]}


def test_companies_rls_roles_and_audit(context: AppContext, tmp_path: Path):
    a = add_company(context.companies, ruc=COMPANY_A, name="Empresa A")
    b = add_company(context.companies, ruc=COMPANY_B, name="Empresa B")
    _scan(context, tmp_path)
    # El comprobante dirigido a otro RUC queda sin empresa y con advertencia.
    with context.db.connect() as conn:
        rows = conn.exec_driver_sql("SELECT number, company_id FROM invoices ORDER BY number").all()
    assert [(r[0], r[1]) for r in rows] == [
        ("00000001", a["id"]),
        ("00000002", b["id"]),
        ("00000003", None),
    ]
    with context.db.connect() as conn:
        codes = {
            r[0]
            for r in conn.exec_driver_sql(
                "SELECT rule_code FROM validation_issues v JOIN invoices i ON i.id = v.invoice_id "
                "WHERE i.number = '00000003'"
            )
        }
    assert "CUSTOMER_NOT_A_COMPANY" in codes

    add_user(context.users, "admin", password=PASSWORD)
    add_user(context.users, "ana", password=PASSWORD, role="accountant", company_ids=[a["id"]])
    add_user(context.users, "gerente", password=PASSWORD, role="approver")
    add_user(context.users, "auditora", password=PASSWORD, role="auditor")

    with TestClient(create_app(context), base_url="https://invoicebot.test") as client:
        admin = _login(client, "admin")
        everything = _invoices(client, admin)
        assert set(everything) == {"F001-00000001", "F001-00000002", "F001-00000003"}
        client.post("/api/v1/auth/logout", headers=admin)

        # Ana solo ve la empresa A: SQL Server filtra (RLS) en listados, detalle y acciones.
        ana = _login(client, "ana")
        assert set(_invoices(client, ana)) == {"F001-00000001"}
        hidden = everything["F001-00000002"]
        assert client.get(f"/api/v1/invoices/{hidden}").status_code in (400, 404)
        assert client.post(
            f"/api/v1/invoices/{hidden}/approve", json={}, headers=ana
        ).status_code in (400, 404, 409)
        me = client.get("/api/v1/auth/me").json()
        assert me["role"] == "accountant" and [c["ruc"] for c in me["companies"]] == [COMPANY_A]
        assert [c["ruc"] for c in client.get("/api/v1/companies").json()["items"]] == [COMPANY_A]
        own = everything["F001-00000001"]
        res = client.post(f"/api/v1/invoices/{own}/approve", json={"force": True}, headers=ana)
        assert res.status_code == 200, res.text
        # Ana (contabilidad) no puede crear empresas.
        assert (
            client.post(
                "/api/v1/companies", json={"ruc": OTHER, "name": "X"}, headers=ana
            ).status_code
            == 403
        )
        client.post("/api/v1/auth/logout", headers=ana)

        # El aprobador ve todo pero no puede corregir ni subir documentos.
        boss = _login(client, "gerente")
        assert len(_invoices(client, boss)) == 3
        res = client.post(
            f"/api/v1/invoices/{own}/correct",
            json={"changes": {"series": "F002"}},
            headers=boss,
        )
        assert res.status_code == 403
        client.post("/api/v1/auth/logout", headers=boss)

        # Auditoría: solo lectura, con historial (quién hizo cada cambio).
        auditor = _login(client, "auditora")
        history = client.get(f"/api/v1/invoices/{own}/history").json()
        operations = [(v["operation"], v["changed_by"]) for v in history["versions"]]
        assert operations[0][0] == "I" and operations[0][1].startswith("job:documents.scan#")
        assert ("U", "ana") in operations
        assert any(a["action"] == "approve" for a in history["actions"])
        assert client.post("/api/v1/runs", headers=auditor).status_code == 403

    # Claves de API: limitadas a una empresa y con permisos finos.
    key_b, _ = context.users.create_api_key(
        name="erp-b", scope="read", created_by="t", company_id=b["id"]
    )
    upload_only, _ = context.users.create_api_key(
        name="escaner", scope="write", created_by="t", permissions="documents:write"
    )
    with TestClient(create_app(context), base_url="https://invoicebot.test") as client:
        assert set(_invoices(client, {"authorization": f"Bearer {key_b}"})) == {"F001-00000002"}
        res = client.get("/api/v1/invoices", headers={"authorization": f"Bearer {upload_only}"})
        assert res.status_code == 403


# --- SSO con un proveedor OIDC simulado ---------------------------------------------------


class FakeProvider:
    def __init__(self, client_id: str):
        self.client_id = client_id
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        self.jwks = {"keys": [{**jwk, "kid": "k1", "use": "sig", "alg": "RS256"}]}
        self.nonce: str | None = None
        self.claims: dict[str, Any] = {}
        self.token_requests: list[dict[str, list[str]]] = []

    def id_token(self) -> str:
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": self.client_id,
            "iat": now,
            "exp": now + 300,
            "nonce": self.nonce,
            **self.claims,
        }
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": "k1"})

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/.well-known/openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": "https://login.example.test/authorize",
                    "token_endpoint": "https://login.example.test/token",
                    "jwks_uri": "https://login.example.test/keys",
                },
            )
        if url.endswith("/keys"):
            return httpx.Response(200, json=self.jwks)
        if url.endswith("/token"):
            self.token_requests.append(parse_qs(request.content.decode()))
            return httpx.Response(200, json={"id_token": self.id_token(), "access_token": "x"})
        return httpx.Response(404)


def _sso(client: TestClient, provider: FakeProvider, **claims: Any) -> httpx.Response:
    start = client.get("/auth/oidc/login", follow_redirects=False)
    assert start.status_code == 303
    query = parse_qs(urlparse(start.headers["location"]).query)
    assert query["code_challenge_method"] == ["S256"]
    provider.nonce = query["nonce"][0]
    provider.claims = claims
    return client.get(
        f"/auth/oidc/callback?code=abc&state={query['state'][0]}", follow_redirects=False
    )


def test_oidc_login_links_creates_and_rejects(tmp_path: Path, db_env, clean_db):
    config = make_config(
        tmp_path,
        {
            "INVOICEBOT_OIDC_ISSUER": ISSUER,
            "INVOICEBOT_OIDC_CLIENT_ID": "app-123",
            "INVOICEBOT_OIDC_ALLOWED_DOMAINS": "empresa.pe",
            "INVOICEBOT_OIDC_AUTO_CREATE_ROLE": "auditor",
        },
        db_env=db_env,
    )
    ctx = AppContext(config)
    provider = FakeProvider("app-123")
    ctx.__dict__["oidc"] = OidcClient(
        issuer=ISSUER,
        client_id="app-123",
        client_secret=None,
        http=httpx.Client(transport=httpx.MockTransport(provider.handler)),
    )
    add_user(ctx.users, "ana", password=PASSWORD, role="accountant", email="ana@empresa.pe")
    try:
        with TestClient(create_app(ctx), base_url="https://invoicebot.test") as client:
            assert "Entrar con" in client.get("/login").text

            # Usuaria existente: se vincula por correo y entra con su rol.
            res = _sso(client, provider, sub="sub-ana", email="ana@empresa.pe", email_verified=True)
            assert res.status_code == 303 and res.headers["location"] == "/"
            me = client.get("/api/v1/auth/me").json()
            assert me["name"] == "ana" and me["role"] == "accountant"
            assert provider.token_requests[-1]["code_verifier"]
            assert ctx.users.find_by_username("ana")["external_subject"] == "sub-ana"
            client.cookies.clear()

            # Desconocido del dominio permitido: se crea con el rol configurado.
            res = _sso(client, provider, sub="sub-luis", email="luis@empresa.pe", name="Luis")
            assert res.headers["location"] == "/"
            assert client.get("/api/v1/auth/me").json()["role"] == "auditor"
            client.cookies.clear()

            # Otro dominio: rechazado.
            res = _sso(client, provider, sub="sub-x", email="x@gmail.com", email_verified=True)
            assert res.headers["location"] == "/login?error=sso_no_user"

            # Nonce cambiado (repetición de un token): rechazado.
            start = client.get("/auth/oidc/login", follow_redirects=False)
            state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
            provider.nonce = "otro"
            provider.claims = {"sub": "sub-ana", "email": "ana@empresa.pe"}
            res = client.get(f"/auth/oidc/callback?code=abc&state={state}", follow_redirects=False)
            assert res.headers["location"] == "/login?error=sso_failed"

            # El state es de un solo uso.
            res = client.get(f"/auth/oidc/callback?code=abc&state={state}", follow_redirects=False)
            assert res.headers["location"] == "/login?error=sso_expired"
    finally:
        ctx.close()
