from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from invoicebot.application import review
from invoicebot.application.exchange_rates import save_exchange_rates
from invoicebot.application.fetch_mail import fetch_mail
from invoicebot.application.processing import retry_documents, scan_invoices
from invoicebot.application.reports import build_export, find_duplicates
from invoicebot.application.verify_sunat import verify_sunat
from invoicebot.domain.sunat_status import SunatResult
from invoicebot.errors import ReviewError
from invoicebot.infrastructure.db.migration import all_migrations, run_migrations, schema_status
from tests import builders

pytestmark = pytest.mark.db


def issue_codes(repository, invoice_id):
    return {i["rule_code"] for i in repository.get_issues(invoice_id) if not i["resolved_at"]}


def test_migrations_are_idempotent(clean_db):
    assert run_migrations(clean_db).applied == []
    status = schema_status(clean_db)
    assert status["current"] == status["latest"] == max(m.version for m in all_migrations())
    assert not status["pending"]


def test_scan_xml_links_pdf_skips_cdr_and_is_idempotent(deps, tmp_path: Path):
    inbox = tmp_path / "inbox"
    builders.write(inbox / "20100047218-01-F001-00001234.xml", builders.ubl_invoice())
    builders.write(
        inbox / "20100047218-01-F001-00001234.pdf", builders.invoice_pdf(number="00001234")
    )
    builders.write(inbox / "R-20100047218-01-F001-00001234.xml", builders.cdr())
    builders.write(inbox / "notas.txt", b"hola")

    summary = scan_invoices(deps, root=str(inbox))
    assert summary.found == 3 and summary.unsupported
    assert (summary.extracted, summary.from_xml, summary.linked_to_xml, summary.skipped) == (
        1,
        1,
        1,
        1,
    )
    assert summary.valid == 1

    again = scan_invoices(deps, root=str(inbox))
    assert again.queued == 0 and again.already_processed == 3


def test_ai_pdf_duplicates_and_export(deps, tmp_path: Path):
    inbox = tmp_path / "inbox"
    builders.write(inbox / "a/factura.pdf", builders.invoice_pdf())
    builders.write(
        inbox / "b/copia.pdf", builders.invoice_pdf()
    )  # mismo contenido: duplicado exacto
    builders.write(
        inbox / "c/reenvio.pdf", builders.invoice_pdf(total="236.00") + b"\n%reenviado"
    )  # otro archivo, mismo comprobante

    summary = scan_invoices(deps, root=str(inbox), recursive=True)
    assert (
        summary.extracted == 2
        and summary.exact_duplicates == 1
        and summary.possible_duplicates == 1
    )
    dups = find_duplicates(deps)
    assert len(dups["exact"]) == 1 and len(dups["possible"]) == 1

    export = build_export(deps, month="2026-10", fmt="csv")
    assert export.rows == 1  # la repetición del comprobante no se exporta
    assert build_export(deps, month="2026-10", fmt="csv", include_duplicates=True).rows == 2


def test_failed_documents_are_retried(deps, tmp_path: Path):
    inbox = tmp_path / "inbox"
    builders.write(inbox / "roto.pdf", b"%PDF-1.4 roto")
    summary = scan_invoices(deps, root=str(inbox))
    assert summary.failed == 1
    retried = retry_documents(deps, failed=True, pending=False)
    assert retried.retry_skipped and retried.retry_skipped[0]["reason"].startswith(
        "error no recuperable"
    )


def test_review_flow_with_audit(deps, repository, tmp_path: Path):
    inbox = tmp_path / "inbox"
    builders.write(inbox / "f.xml", builders.ubl_invoice(total="120.00"))  # total que no cuadra
    scan_invoices(deps, root=str(inbox))
    invoice = repository.list_invoices_needing_review()[0]
    invoice_id = invoice["invoice_id"]
    assert invoice["validation_status"] == "error"
    assert "TOTAL_MISMATCH" in issue_codes(repository, invoice_id)

    with pytest.raises(ReviewError):
        review.approve_invoice(deps, invoice_id, user="ana")
    result = review.correct_invoice(
        deps, invoice_id, changes=["total=118.00"], user="ana", note="según PDF"
    )
    assert result["validation_status"] == "valid" and result["changed"][0]["from"] == "120.00"
    approved = review.approve_invoice(deps, invoice_id, user="ana")
    assert approved["review_status"] == "approved"
    review.reopen_invoice(deps, invoice_id, user="ana")
    review.reject_invoice(
        deps, invoice_id, user="ana", reason="Comprobante anulado por el proveedor"
    )
    with pytest.raises(ReviewError):
        review.correct_invoice(deps, invoice_id, changes=["total=1"], user="ana")
    actions = [a["action"] for a in repository.list_review_actions(invoice_id)]
    assert actions == ["correct", "approve", "reopen", "reject"]
    detail = repository.get_invoice_detail(invoice_id)
    assert detail["corrected"] and detail["review_status"] == "rejected"
    assert build_export(deps, fmt="csv").rows == 0


def test_dollar_invoice_waits_for_exchange_rate(deps, repository, tmp_path: Path):
    inbox = tmp_path / "inbox"
    builders.write(inbox / "usd.xml", builders.ubl_invoice(currency="USD", issue_date="2026-10-17"))
    scan_invoices(deps, root=str(inbox))
    invoice_id = repository.list_invoices_needing_review()[0]["invoice_id"]
    assert "FX_RATE_MISSING" in issue_codes(repository, invoice_id)

    # Publicado el viernes 16: el sábado 17 usa ese.
    result = save_exchange_rates(
        deps,
        [{"currency": "USD", "date": "2026-10-16", "buy": 3.74, "sell": 3.75, "source": "manual"}],
    )
    assert result["updated"] == 1
    detail = repository.get_invoice_detail(invoice_id)
    assert float(detail["exchange_rate"]) == 3.75 and detail["validation_status"] == "valid"
    # Una descarga de SUNAT no pisa el tipo de cambio cargado a mano.
    save_exchange_rates(
        deps, [{"currency": "USD", "date": "2026-10-16", "buy": 1, "sell": 9.99, "source": "sunat"}]
    )
    assert repository.find_exchange_rate("USD", "2026-10-16").rate == 3.75


def test_sunat_verification(deps, repository, tmp_path: Path):
    inbox = tmp_path / "inbox"
    builders.write(inbox / "a.xml", builders.ubl_invoice(doc_id="F001-1"))
    builders.write(inbox / "b.xml", builders.ubl_invoice(doc_id="F001-2"))
    scan_invoices(deps, root=str(inbox))

    class FakeSunat:
        def validate_receipt(self, **query):
            return SunatResult("2" if query["number"] == "2" else "1", "00", "00")

    summary = verify_sunat(deps, client=FakeSunat())
    assert (summary.checked, summary.accepted, summary.voided) == (
        2,
        1,
        1,
    ) and summary.has_problems()
    voided = next(r for r in repository.search_invoices()[0] if r["number"] == "2")
    assert voided["sunat_status"] == "ANULADO" and voided["validation_status"] == "error"
    assert verify_sunat(deps, client=FakeSunat()).candidates == 0  # ya consultados


def test_mail_fetch_saves_attachments_once(deps, repository, tmp_path: Path):
    deps.config = replace(
        deps.config,
        mail=replace(deps.config.mail, processed_folder="Procesado", review_folder="Revisar"),
    )
    source = builders.FakeMailSource(
        {
            "m1": builders.mail_message(
                attachments={
                    "F001-9.xml": (builders.ubl_invoice(doc_id="F001-9"), "application/xml")
                }
            ),
            "m2": builders.mail_message(
                subject="Hola", attachments={"cv.docx": (b"PK", "application/octet-stream")}
            ),
        }
    )
    mail_dir = tmp_path / "inbox" / "correo"
    summary = fetch_mail(deps, source=source, directory=str(mail_dir))
    assert (summary.read, summary.saved, summary.files, summary.no_attachments) == (2, 1, 1, 1)
    assert sorted(source.moved) == [("m1", "Procesado"), ("m2", "Revisar")] and source.closed
    assert list(mail_dir.rglob("F001-9.xml"))
    again = fetch_mail(
        deps, source=builders.FakeMailSource(source.messages), directory=str(mail_dir)
    )
    assert again.read == 0

    scan_invoices(deps, root=str(tmp_path / "inbox"), recursive=True)
    document = repository.list_documents_by_status(["extracted"])[0]
    origins = repository.find_mail_origins(document["sha256"])
    assert origins[0]["sender"] == "facturacion@proveedor.pe"
