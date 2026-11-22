"""Detección de duplicados por clave de negocio."""

from __future__ import annotations

import re
import unicodedata

from invoicebot.domain.invoice import FOREIGN_DOCUMENT_TYPE, InvoiceData


class DuplicateKind:
    #: Mismo contenido binario (SHA-256) en otra ruta.
    EXACT = "exact"
    #: Archivos distintos con la misma clave de negocio.
    POSSIBLE = "possible"


def normalize_name(name: str) -> str:
    """ "Cloudflare, Inc." y "CLOUDFLARE INC" dan el mismo emisor."""
    decomposed = unicodedata.normalize("NFD", str(name))
    without_marks = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"[^A-Z0-9]+", " ", without_marks.upper()).strip()


def business_key(invoice: InvoiceData) -> str | None:
    """Clave de negocio: RUC emisor + tipo + serie + número.

    El número se compara sin ceros a la izquierda ("00001234" = "1234"). Un
    comprobante del exterior se identifica por el identificador fiscal del
    proveedor (o su nombre) y el número. None si falta alguna parte: sin clave
    completa no se puede afirmar que dos comprobantes sean el mismo.
    """
    if invoice.document_type == FOREIGN_DOCUMENT_TYPE:
        supplier: str | None = None
        if invoice.supplier_tax_id:
            supplier = "ID:" + re.sub(r"[^A-Z0-9]", "", invoice.supplier_tax_id.upper())
        elif invoice.supplier_name:
            supplier = "NOMBRE:" + normalize_name(invoice.supplier_name)
        if not supplier or not invoice.number:
            return None
        return "|".join((supplier, invoice.document_type, invoice.number.upper()))
    if not (invoice.supplier_ruc and invoice.document_type and invoice.series and invoice.number):
        return None
    number = re.sub(r"^0+(?=\d)", "", invoice.number)
    return "|".join((invoice.supplier_ruc, invoice.document_type, invoice.series.upper(), number))
