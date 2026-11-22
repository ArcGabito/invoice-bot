from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from invoicebot.config import load_config
from invoicebot.errors import ConfigError, DocumentReadError, ExtractionError
from invoicebot.infrastructure.ai.anthropic_provider import AnthropicProvider
from invoicebot.infrastructure.ai.mock_provider import MockProvider
from invoicebot.infrastructure.ai.ollama_provider import OllamaProvider, parse_chat_response
from invoicebot.infrastructure.ai.schema import Extraction, empty_extraction
from invoicebot.infrastructure.db.connection import build_target, parse_connection_string
from invoicebot.infrastructure.documents.pdf import extract_pdf_text, resolve_unmapped_glyphs
from invoicebot.infrastructure.documents.router import DocumentRouter
from invoicebot.infrastructure.documents.ubl import parse_ubl
from invoicebot.infrastructure.exports import EXPORT_COLUMNS, render_csv, render_xlsx
from invoicebot.infrastructure.mail.attachments import extract_mail_files, safe_file_name
from invoicebot.infrastructure.mail.sources import has_attachment_structure
from invoicebot.infrastructure.security import hash_password, verify_password
from invoicebot.infrastructure.sunat import (
    SunatClient,
    SunatError,
    parse_exchange_rate_csv,
    parse_sunat_exchange_rate,
)
from tests import builders

#: Generado con la versión 0.x (Node.js crypto.scrypt): los usuarios conservan su contraseña.
NODE_HASH = "scrypt$32768$8$1$MDEyMzQ1Njc4OWFiY2RlZg==$PSOiB+cG93eNoGYNAKbi+kStQuhTyTelfXjTN4f98bU="


class TestUbl:
    def test_invoice(self):
        doc = parse_ubl(builders.ubl_invoice())
        assert doc.kind == "invoice"
        d = doc.data
        assert (d["supplier_ruc"], d["series"], d["number"], d["document_type"]) == (
            "20100047218",
            "F001",
            "00001234",
            "01",
        )
        assert (d["taxable_amount"], d["igv"], d["total"], d["currency"]) == (
            "100.00",
            "18.00",
            "118.00",
            "PEN",
        )

    def test_detraction_credit_note_and_cdr(self):
        doc = parse_ubl(
            builders.ubl_invoice(detraction={"code": "037", "percent": "12", "amount": "142.00"})
        )
        assert (
            doc.data["detraction_code"],
            doc.data["detraction_percent"],
            doc.data["detraction_amount"],
        ) == ("037", "12", "142.00")
        assert doc.data["detraction_account"] == "00-000-123456"
        assert parse_ubl(builders.ubl_invoice(root="CreditNote")).data["document_type"] == "07"
        assert parse_ubl(builders.cdr()).kind == "cdr"
        assert parse_ubl(b"<Catalogo/>").kind == "other"

    def test_malformed_and_entities(self):
        with pytest.raises(DocumentReadError):
            parse_ubl(b"<Invoice>")
        bomb = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><Invoice>&a;</Invoice>'
        with pytest.raises(DocumentReadError):
            parse_ubl(bomb)

    def test_latin1(self):
        xml = (
            builders.ubl_invoice(name="CAFÉ ÑAÑO")
            .decode()
            .replace('encoding="UTF-8"', 'encoding="ISO-8859-1"')
        )
        assert parse_ubl(xml.encode("latin-1")).data["supplier_name"] == "CAFÉ ÑAÑO"


class TestPdf:
    def test_text_and_router(self, tmp_path: Path):
        path = builders.write(tmp_path / "f.pdf", builders.invoice_pdf())
        text = extract_pdf_text(str(path))
        assert text.page_count == 1 and "F001-00000777" in text.text
        router = DocumentRouter(min_text_chars=40)
        result = router.extract_text(file_path=str(path), mime_type="application/pdf")
        assert result.method == "pdf_text" and not result.needs_ocr
        image = builders.write(tmp_path / "foto.png", b"\x89PNG\r\n\x1a\n")
        assert router.extract_text(file_path=str(image), mime_type="image/png").needs_ocr

    def test_damaged(self, tmp_path: Path):
        path = builders.write(tmp_path / "roto.pdf", b"%PDF-1.4 basura")
        with pytest.raises(DocumentReadError):
            extract_pdf_text(str(path))

    def test_unmapped_glyphs(self):
        assert resolve_unmapped_glyphs("IN\x0045946823") == "IN-45946823"
        assert resolve_unmapped_glyphs("Total \x00 10") == "Total 10"


class TestProviders:
    def test_mock(self):
        text = extract_pdf_text_from_bytes(builders.invoice_pdf())
        data = MockProvider().extract(text=text, file_name="f.pdf").data
        assert (data["supplier_ruc"], data["series"], data["number"], data["total"]) == (
            "20100047218",
            "F001",
            "00000777",
            "236.00",
        )
        assert data["issue_date"] == "2026-10-15" and data["is_invoice"]

    def test_ollama(self):
        payload = {**empty_extraction(), "is_invoice": True, "total": "10.00"}
        chunks = [
            {"message": {"content": json.dumps(payload)[:20]}, "done": False},
            {
                "message": {"content": json.dumps(payload)[20:]},
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 5,
                "eval_count": 7,
                "model": "qwen",
            },
        ]
        assert (
            parse_chat_response("\n".join(json.dumps(c) for c in chunks)).usage.output_tokens == 7
        )

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert body["format"]["type"] == "object" and body["think"] is False
            return httpx.Response(
                200,
                json={"message": {"content": json.dumps(payload)}, "done": True, "model": "qwen"},
            )

        provider = OllamaProvider(
            url="http://ollama",
            model="qwen",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        assert provider.extract(text="x", file_name="a.pdf").data["total"] == "10.00"

        bad = OllamaProvider(
            url="http://ollama",
            model="qwen",
            client=httpx.Client(
                transport=httpx.MockTransport(
                    lambda r: httpx.Response(
                        200, json={"message": {"content": "{no"}, "done": True}
                    )
                )
            ),
        )
        with pytest.raises(ExtractionError) as error:
            bad.extract(text="x", file_name="a.pdf")
        assert error.value.kind == "invalid_output" and error.value.retryable

    def test_anthropic_request_shape_and_refusal(self):
        calls = []
        parsed = Extraction.model_validate(
            {**empty_extraction(), "is_invoice": True, "total": "5.00"}
        )

        def parse(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text="{}")],
                usage=SimpleNamespace(input_tokens=10, output_tokens=20),
                stop_reason=kwargs.get("_stop", "end_turn"),
                parsed_output=parsed,
                model="claude-opus-5-5",
            )

        client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(parse=parse)))
        provider = AnthropicProvider(model="claude-opus-5-5", effort="medium", client=client)
        result = provider.extract(text="FACTURA", file_name="f.pdf")
        assert result.data["total"] == "5.00" and result.usage.output_tokens == 20
        sent = calls[0]
        assert sent["fallbacks"] == "default" and sent["betas"] == [
            "server-side-fallback-2026-07-01"
        ]
        assert sent["output_format"] is Extraction and sent["output_config"] == {"effort": "medium"}

        def refuse(**kwargs):
            return SimpleNamespace(
                content=[],
                usage=None,
                stop_reason="refusal",
                stop_details=SimpleNamespace(category="cyber"),
                parsed_output=None,
                model="m",
            )

        client.beta.messages.parse = refuse
        with pytest.raises(ExtractionError) as error:
            provider.extract(text="x", file_name="f.pdf")
        assert error.value.kind == "refused" and not error.value.retryable


def extract_pdf_text_from_bytes(data: bytes) -> str:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = builders.write(Path(tmp) / "x.pdf", data)
        return extract_pdf_text(str(path)).text


class TestSunat:
    def test_client(self):
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if "oauth2/token" in str(request.url):
                return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
            body = json.loads(request.content)
            assert body == {
                "numRuc": "20100047218",
                "codComp": "01",
                "numeroSerie": "F001",
                "numero": 1234,
                "fechaEmision": "15/10/2026",
                "monto": 118.0,
            }
            assert request.headers["authorization"] == "Bearer tok"
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {"estadoCp": 1, "estadoRuc": "00", "condDomiRuc": "00"},
                },
            )

        client = SunatClient(
            ruc="20601234565",
            client_id="id",
            client_secret="secret",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        result = client.validate_receipt(
            supplier_ruc="20100047218",
            document_type="01",
            series="F001",
            number="00001234",
            issue_date="2026-10-15",
            total_cents=11800,
        )
        assert result.estado_cp == "1"
        client.validate_receipt(
            supplier_ruc="20100047218",
            document_type="01",
            series="F001",
            number="1234",
            issue_date="2026-10-15",
            total_cents=11800,
        )
        assert sum("oauth2" in str(r.url) for r in seen) == 1  # token reutilizado

    def test_bad_credentials(self):
        client = SunatClient(
            ruc="1",
            client_id="id",
            client_secret="x",
            client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401))),
        )
        with pytest.raises(SunatError) as error:
            client.get_token()
        assert error.value.kind == "sunat_auth"

    def test_exchange_rate(self):
        rate = parse_sunat_exchange_rate("31/10/2026|3.458|3.467|")
        assert (rate["date"], rate["buy"], rate["sell"]) == ("2026-10-31", 3.458, 3.467)
        rates, errors = parse_exchange_rate_csv(
            "fecha;compra;venta\n01/10/2026;3.70;3.71\n2026-10-02;3.72\n30/02/2026;1;1\n"
        )
        assert [r["date"] for r in rates] == ["2026-10-01", "2026-10-02"]
        assert rates[1]["buy"] is None and len(errors) == 1


class TestMail:
    def test_attachments_zip_and_logos(self):
        raw = builders.mail_message(
            attachments={
                "F001-1.xml": (builders.ubl_invoice(), "application/xml"),
                "../../evil.pdf": (builders.invoice_pdf(), "application/pdf"),
                "comprobantes.zip": (
                    builders.zip_bytes(
                        {"a/F001-2.xml": b"<x/>", "__MACOSX/._a": b"", "nota.txt": b"x"}
                    ),
                    "application/zip",
                ),
                "contrato.docx": (
                    b"PK",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ),
                "firma.png": (b"\x89PNG" + b"0" * 100, "image/png"),
            },
            inline_image=b"\x89PNG" + b"0" * 50_000,
        )
        mail = extract_mail_files(raw)
        assert mail.sender == "facturacion@proveedor.pe" and mail.subject == "Factura F001-1234"
        assert sorted(f.name for f in mail.files) == ["F001-1.xml", "F001-2.xml", "evil.pdf"]
        assert any("docx" in s for s in mail.skipped) and any("pequeña" in s for s in mail.skipped)

    def test_safe_names_and_structure(self):
        assert safe_file_name("..\\..\\con.pdf") == "adjunto.pdf"
        assert safe_file_name("fa<ct>ura?.PDF") == "fa_ct_ura_.pdf"
        assert has_attachment_structure(
            '("APPLICATION" "PDF" ("NAME" "f.pdf") NIL NIL "BASE64" 100 NIL ("ATTACHMENT" ("FILENAME" "f.pdf")))'
        )
        assert not has_attachment_structure(
            '("TEXT" "PLAIN" ("CHARSET" "utf-8") NIL NIL "7BIT" 10 1)'
        )


class TestConfigAndSecurity:
    def test_ado_connection_string(self):
        raw = (
            r"Data Source=TUF-F16\GABODB;Integrated Security=True;Persist Security Info=False;Pooling=False;"
            r"MultipleActiveResultSets=False;Encrypt=True;TrustServerCertificate=True;"
            r'Application Name="SQL Server Management Studio";Command Timeout=0'
        )
        assert parse_connection_string(raw)["application name"] == "SQL Server Management Studio"
        config = load_config({"INVOICEBOT_DB_CONNECTION_STRING": raw})
        target = build_target(config.db, config.env)
        assert target.auth == "windows" and target.database == "invoicebot"
        assert "SERVER=TUF-F16\\GABODB" in target.connection_string
        assert (
            "TRUSTED_CONNECTION=yes" in target.connection_string
            and "TRUSTSERVERCERTIFICATE=yes" in target.connection_string
        )
        assert build_target(config.db, config.env, database="master").database == "master"

    def test_parts_and_managed_identity(self):
        sql = load_config(
            {
                "INVOICEBOT_DB_SERVER": "db",
                "INVOICEBOT_DB_USER": "app",
                "INVOICEBOT_DB_PASSWORD": "p;w{d}",
            }
        )
        target = build_target(sql.db, sql.env)
        assert (
            "SERVER=db,1433" in target.connection_string
            and "PWD={p;w{d}}}" in target.connection_string
        )
        mi = load_config(
            {
                "INVOICEBOT_DB_SERVER": "x.database.windows.net",
                "INVOICEBOT_DB_AUTH": "managed-identity",
                "AZURE_CLIENT_ID": "abc",
            }
        )
        assert "AUTHENTICATION=ActiveDirectoryMsi" in build_target(mi.db, mi.env).connection_string
        named = load_config({"INVOICEBOT_DB_SERVER": r"PC\SQLEXPRESS"})
        assert named.db.port is None and named.db.auth == "windows"

    def test_invalid_config_lists_every_problem(self):
        with pytest.raises(ConfigError) as error:
            load_config(
                {
                    "INVOICEBOT_CONCURRENCY": "99",
                    "INVOICEBOT_OCR_ENGINE": "magia",
                    "INVOICEBOT_SUNAT_VERIFY_ON_SCAN": "true",
                }
            )
        message = error.value.message
        assert (
            "INVOICEBOT_CONCURRENCY" in message
            and "INVOICEBOT_OCR_ENGINE" in message
            and "VERIFY_ON_SCAN" in message
        )

    def test_passwords(self):
        stored = hash_password("una-clave-larga-123")
        assert verify_password("una-clave-larga-123", stored) and not verify_password(
            "otra", stored
        )
        assert verify_password("Contraseña-segura-2026", NODE_HASH)
        assert not verify_password("x", "bcrypt$nope")


def test_exports():
    row = {
        "id": 1,
        "issue_date": __import__("datetime").date(2026, 10, 15),
        "supplier_ruc": "20100047218",
        "series": "F001",
        "number": "00001234",
        "currency": "USD",
        "total_cents": 11800,
        "taxable_amount_cents": 10000,
        "igv_cents": 1800,
        "detraction_amount_cents": 4425,
        "exchange_rate": __import__("decimal").Decimal("3.7500"),
        "validation_status": "valid",
    }
    csv = render_csv([row], bom=True).decode("utf-8-sig").splitlines()
    assert csv[0].split(",")[: len(EXPORT_COLUMNS)][0] == "invoice_id"
    values = dict(zip(csv[0].split(","), csv[1].split(","), strict=True))
    assert (
        values["number"] == "00001234"
        and values["total"] == "118.00"
        and values["total_pen"] == "442.50"
    )
    assert values["exchange_rate"] == "3.75" and values["net_payable"] == "106.20"
    assert render_xlsx([row], title="t")[:2] == b"PK"
