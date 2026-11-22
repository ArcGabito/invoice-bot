"""Generadores de comprobantes ficticios para las pruebas (XML UBL, PDF y correos)."""

from __future__ import annotations

import io
import zipfile
from email.message import EmailMessage
from pathlib import Path
from typing import Any


def ubl_invoice(
    *,
    ruc: str = "20100047218",
    name: str = "PROVEEDOR DEMO S.A.C.",
    customer_ruc: str = "20601234565",
    customer: str = "CLIENTE DEMO S.A.",
    doc_id: str = "F001-00001234",
    type_code: str = "01",
    issue_date: str = "2026-10-15",
    currency: str = "PEN",
    taxable: str = "100.00",
    igv: str = "18.00",
    total: str = "118.00",
    root: str = "Invoice",
    detraction: dict[str, str] | None = None,
    extra_taxes: str = "",
) -> bytes:
    """Factura UBL 2.1 con la estructura que exige SUNAT (lo que lee el parser)."""
    payment = ""
    if detraction:
        payment = f"""
  <cac:PaymentMeans><cbc:ID>Detraccion</cbc:ID><cbc:PaymentMeansCode>001</cbc:PaymentMeansCode>
    <cac:PayeeFinancialAccount><cbc:ID>{detraction.get("account", "00-000-123456")}</cbc:ID></cac:PayeeFinancialAccount>
  </cac:PaymentMeans>
  <cac:PaymentTerms><cbc:ID>Detraccion</cbc:ID><cbc:PaymentMeansID>{detraction["code"]}</cbc:PaymentMeansID>
    <cbc:PaymentPercent>{detraction["percent"]}</cbc:PaymentPercent>
    <cbc:Amount currencyID="PEN">{detraction["amount"]}</cbc:Amount>
  </cac:PaymentTerms>"""
    type_code_xml = (
        f'<cbc:InvoiceTypeCode listID="0101">{type_code}</cbc:InvoiceTypeCode>'
        if root == "Invoice"
        else ""
    )
    totals = "LegalMonetaryTotal" if root != "DebitNote" else "RequestedMonetaryTotal"
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<{root} xmlns="urn:oasis:names:specification:ubl:schema:xsd:{root}-2"
  xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
  xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:UBLVersionID>2.1</cbc:UBLVersionID>
  <cbc:ID>{doc_id}</cbc:ID>
  <cbc:IssueDate>{issue_date}</cbc:IssueDate>
  {type_code_xml}
  <cbc:DocumentCurrencyCode>{currency}</cbc:DocumentCurrencyCode>
  <cac:AccountingSupplierParty><cac:Party>
    <cac:PartyIdentification><cbc:ID schemeID="6">{ruc}</cbc:ID></cac:PartyIdentification>
    <cac:PartyLegalEntity><cbc:RegistrationName>{name}</cbc:RegistrationName></cac:PartyLegalEntity>
  </cac:Party></cac:AccountingSupplierParty>
  <cac:AccountingCustomerParty><cac:Party>
    <cac:PartyIdentification><cbc:ID schemeID="6">{customer_ruc}</cbc:ID></cac:PartyIdentification>
    <cac:PartyLegalEntity><cbc:RegistrationName>{customer}</cbc:RegistrationName></cac:PartyLegalEntity>
  </cac:Party></cac:AccountingCustomerParty>{payment}
  <cac:TaxTotal>
    <cbc:TaxAmount currencyID="{currency}">{igv}</cbc:TaxAmount>
    <cac:TaxSubtotal>
      <cbc:TaxableAmount currencyID="{currency}">{taxable}</cbc:TaxableAmount>
      <cbc:TaxAmount currencyID="{currency}">{igv}</cbc:TaxAmount>
      <cac:TaxCategory><cac:TaxScheme><cbc:ID>1000</cbc:ID><cbc:Name>IGV</cbc:Name></cac:TaxScheme></cac:TaxCategory>
    </cac:TaxSubtotal>{extra_taxes}
  </cac:TaxTotal>
  <cac:{totals}>
    <cbc:LineExtensionAmount currencyID="{currency}">{taxable}</cbc:LineExtensionAmount>
    <cbc:PayableAmount currencyID="{currency}">{total}</cbc:PayableAmount>
  </cac:{totals}>
</{root}>"""
    return xml.encode("utf-8")


def cdr() -> bytes:
    return b"""<?xml version="1.0" encoding="UTF-8"?>
<ar:ApplicationResponse xmlns:ar="urn:oasis:names:specification:ubl:schema:xsd:ApplicationResponse-2">
  <cbc:ID xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">1</cbc:ID>
</ar:ApplicationResponse>"""


def _pdf_escape(text: str) -> bytes:
    raw = text.encode("cp1252", errors="replace")
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def text_pdf(lines: list[str]) -> bytes:
    """PDF mínimo con texto nativo (Helvetica, WinAnsi): lo que produce un sistema de facturación."""
    content = (
        b"BT /F1 11 Tf 50 800 Td 14 TL\n"
        + b"".join(b"(" + _pdf_escape(line) + b") '\n" for line in lines)
        + b"ET"
    )
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return out.getvalue()


def invoice_pdf(
    *,
    ruc: str = "20100047218",
    series: str = "F001",
    number: str = "00000777",
    total: str = "236.00",
    taxable: str = "200.00",
    igv: str = "36.00",
    issue_date: str = "15/10/2026",
) -> bytes:
    """Representación impresa con las etiquetas que entiende el proveedor simulado."""
    return text_pdf(
        [
            "COMERCIAL ANDINA S.A.C.",
            f"RUC: {ruc}",
            "FACTURA ELECTRONICA",
            f"{series}-{number}",
            f"Fecha de emision: {issue_date}",
            "Senor(es): CLIENTE DEMO S.A.",
            "RUC: 20601234565",
            "Moneda: Soles S/",
            f"Op. Gravadas S/ {taxable}",
            f"IGV 18% S/ {igv}",
            f"Importe Total S/ {total}",
        ]
    )


def write(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def zip_bytes(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def mail_message(
    *,
    sender: str = "facturacion@proveedor.pe",
    subject: str = "Factura F001-1234",
    attachments: dict[str, tuple[bytes, str]] | None = None,
    inline_image: bytes | None = None,
) -> bytes:
    message = EmailMessage()
    message["From"] = f"Proveedor <{sender}>"
    message["To"] = "facturas@empresa.pe"
    message["Subject"] = subject
    message["Date"] = "Thu, 15 Oct 2026 10:00:00 -0500"
    message.set_content("Adjuntamos su comprobante.")
    if inline_image is not None:
        message.add_alternative("<p>Logo <img src='cid:logo'></p>", subtype="html")
        html = message.get_payload()[1]  # type: ignore[index]
        html.add_related(inline_image, maintype="image", subtype="png", cid="<logo>")
    for name, (content, mime) in (attachments or {}).items():
        maintype, subtype = mime.split("/")
        message.add_attachment(content, maintype=maintype, subtype=subtype, filename=name)
    return bytes(message)


class FakeMailSource:
    """Buzón en memoria con la misma interfaz que los conectores reales."""

    provider = "fake"

    def __init__(self, messages: dict[str, bytes], address: str = "facturas@empresa.pe"):
        from datetime import UTC, datetime

        self.messages = messages
        self.address = address
        self.moved: list[tuple[str, str]] = []
        self.closed = False
        self._received = datetime(2026, 10, 15, 15, 0, tzinfo=UTC)

    def connect(self) -> str:
        return self.address

    def list_messages(self, *, since: Any) -> list[Any]:
        from invoicebot.application.ports import MailMessageRef

        return [MailMessageRef(mid, self._received) for mid in self.messages]

    def fetch_raw(self, message_id: str) -> bytes:
        return self.messages[message_id]

    def move(self, message_id: str, folder: str) -> None:
        self.moved.append((message_id, folder))

    def close(self) -> None:
        self.closed = True
