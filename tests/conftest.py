"""Fixtures compartidas.

Las pruebas de integración usan un SQL Server real: cada sesión de pytest crea su
propia base `invoicebot_test_<aleatorio>` y la borra al terminar. La conexión se
lee de INVOICEBOT_TEST_DB_CONNECTION_STRING (ODBC o formato SSMS) o de
INVOICEBOT_TEST_DB_SERVER/_PORT/_USER/_PASSWORD/_TRUST_CERT, del entorno o de un
archivo .env.test. Sin conexión, esas pruebas se omiten (o fallan con
INVOICEBOT_TEST_DB_REQUIRED=true, como en CI).
"""

from __future__ import annotations

import logging
import os
import secrets
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from dotenv import dotenv_values

from invoicebot.application.deps import Deps
from invoicebot.config import Config, load_config
from invoicebot.infrastructure.ai.mock_provider import MockProvider
from invoicebot.infrastructure.db.admin import create_empty_database, drop_database
from invoicebot.infrastructure.db.connection import Database
from invoicebot.infrastructure.db.invoice_repository import InvoiceRepository
from invoicebot.infrastructure.db.migration import run_migrations
from invoicebot.infrastructure.db.user_repository import UserRepository
from invoicebot.infrastructure.documents.router import DocumentRouter

ROOT = Path(__file__).resolve().parents[1]
TODAY = "2026-10-31"

_TABLES = (
    "webhook_deliveries",
    "outbox_events",
    "webhooks",
    "approval_requests",
    "approval_rules",
    "approval_delegations",
    "supplier_accounting",
    "invoice_matches",
    "goods_receipt_lines",
    "goods_receipts",
    "purchase_order_lines",
    "purchase_orders",
    "jobs",
    "schedules",
    "mail_attachments",
    "mail_messages",
    "review_sessions",
    "api_keys",
    "users",
    "review_actions",
    "sunat_checks",
    "validation_issues",
    "invoice_changes",
    "invoices",
    "user_companies",
    "companies",
    "company_changes",
    "oidc_requests",
    "extraction_runs",
    "file_occurrences",
    "documents",
    "bot_runs",
    "exchange_rates",
)


def _test_env() -> dict[str, str]:
    values: dict[str, str] = {}
    env_file = ROOT / ".env.test"
    if env_file.is_file():
        values.update({k: v for k, v in dotenv_values(env_file).items() if v is not None})
    values.update({k: v for k, v in os.environ.items() if k.startswith("INVOICEBOT_TEST_")})
    return values


def _db_env(test: dict[str, str], name: str) -> dict[str, str] | None:
    env: dict[str, str] = {"INVOICEBOT_DB_NAME": name}
    if test.get("INVOICEBOT_TEST_DB_CONNECTION_STRING"):
        env["INVOICEBOT_DB_CONNECTION_STRING"] = test["INVOICEBOT_TEST_DB_CONNECTION_STRING"]
        return env
    if not test.get("INVOICEBOT_TEST_DB_SERVER"):
        return None
    env["INVOICEBOT_DB_SERVER"] = test["INVOICEBOT_TEST_DB_SERVER"]
    if test.get("INVOICEBOT_TEST_DB_PORT"):
        env["INVOICEBOT_DB_PORT"] = test["INVOICEBOT_TEST_DB_PORT"]
    if test.get("INVOICEBOT_TEST_DB_USER"):
        env["INVOICEBOT_DB_AUTH"] = "sql"
        env["INVOICEBOT_DB_USER"] = test["INVOICEBOT_TEST_DB_USER"]
        env["INVOICEBOT_DB_PASSWORD"] = test.get("INVOICEBOT_TEST_DB_PASSWORD", "")
    else:
        env["INVOICEBOT_DB_AUTH"] = "windows"
    env["INVOICEBOT_DB_TRUST_CERT"] = test.get("INVOICEBOT_TEST_DB_TRUST_CERT", "false")
    return env


def make_config(
    tmp_path: Path, extra: dict[str, str] | None = None, db_env: dict[str, str] | None = None
) -> Config:
    env = {
        "INVOICEBOT_AI_PROVIDER": "mock",
        "INVOICEBOT_OCR_ENGINE": "none",
        "INVOICEBOT_DATA_DIR": str(tmp_path / "data"),
        "INVOICEBOT_EXPORT_DIR": str(tmp_path / "exports"),
        "INVOICEBOT_INBOX_DIR": str(tmp_path / "inbox"),
        "INVOICEBOT_FX_SUNAT": "false",
        "INVOICEBOT_CONCURRENCY": "2",
        "INVOICEBOT_LOG_LEVEL": "warning",
        **(db_env or {}),
        **(extra or {}),
    }
    return load_config(env, cwd=tmp_path)


@pytest.fixture(scope="session")
def db_env() -> Iterator[dict[str, str]]:
    test = _test_env()
    name = f"invoicebot_test_{secrets.token_hex(4)}"
    env = _db_env(test, name)
    if env is None:
        if (
            test.get("INVOICEBOT_TEST_DB_REQUIRED", os.environ.get("INVOICEBOT_TEST_DB_REQUIRED"))
            == "true"
        ):
            pytest.fail("Falta la conexión de pruebas (INVOICEBOT_TEST_DB_*)")
        pytest.skip("Sin SQL Server de pruebas: define INVOICEBOT_TEST_DB_* o .env.test")
    config = load_config({**env, "INVOICEBOT_AI_PROVIDER": "mock"})
    master = Database.from_config(config.db, config.env, database="master", pooled=False)
    create_empty_database(master, name, replace=True)
    database = Database.from_config(config.db, config.env)
    run_migrations(database)
    database.dispose()
    yield env
    drop_database(master, name)
    master.dispose()


@pytest.fixture(scope="session")
def database(db_env: dict[str, str]) -> Iterator[Database]:
    config = load_config({**db_env, "INVOICEBOT_AI_PROVIDER": "mock"})
    db = Database.from_config(config.db, config.env)
    yield db
    db.dispose()


@pytest.fixture
def clean_db(database: Database) -> Database:
    with database.begin() as conn:
        for table in _TABLES:
            conn.exec_driver_sql(f"DELETE FROM {table}")
    return database


@pytest.fixture
def config(tmp_path: Path, db_env: dict[str, str]) -> Config:
    return make_config(tmp_path, db_env=db_env)


@pytest.fixture
def repository(clean_db: Database) -> InvoiceRepository:
    return InvoiceRepository(clean_db)


@pytest.fixture
def users(clean_db: Database) -> UserRepository:
    return UserRepository(clean_db)


def build_deps(config: Config, repository: InvoiceRepository, **overrides: Any) -> Deps:
    return Deps(
        config=config,
        repository=repository,
        document_router=overrides.get("document_router")
        or DocumentRouter(min_text_chars=config.min_text_chars),
        ai_provider=overrides.get("ai_provider") or MockProvider(),
        vision_extractor=overrides.get("vision_extractor"),
        today=overrides.get("today", TODAY),
        logger=logging.getLogger("invoicebot.tests"),
    )


@pytest.fixture
def deps(config: Config, repository: InvoiceRepository) -> Deps:
    return build_deps(config, repository)
