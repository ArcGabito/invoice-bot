"""Proveedor sin IA basado en expresiones regulares.

Sirve para pruebas automatizadas y para probar el flujo completo sin coste ni
conexión. Solo reconoce comprobantes con etiquetas convencionales.
"""

from __future__ import annotations

import json
import re

from invoicebot.application.ports import AiExtractionResult, AiUsage
from invoicebot.infrastructure.ai.schema import Extraction, empty_extraction


class MockProvider:
    name = "mock"
    model = "regex-heuristics-v1"

    def extract(self, *, text: str, file_name: str = "", ocr: bool = False) -> AiExtractionResult:
        data = empty_extraction()
        lines = [line.strip() for line in text.split("\n") if line.strip()]

        def find(pattern: str) -> str | None:
            m = re.search(pattern, text, re.IGNORECASE)
            return m[1].strip() if m else None

        def amount_after(label: str) -> str | None:
            return find(rf"{label}[^\d\n]*([\d.,]+)")

        data["is_invoice"] = bool(
            re.search(
                r"factura|boleta|nota de (cr[ée]dito|d[ée]bito)|comprobante", text, re.IGNORECASE
            )
        )
        rucs = re.findall(r"R\.?U\.?C\.?\s*(?:N[°º.o]*)?\s*:?\s*(\d{11})", text, re.IGNORECASE)
        data["supplier_ruc"] = rucs[0] if rucs else None
        data["customer_ruc"] = rucs[1] if len(rucs) > 1 else find(r"DNI\s*:?\s*(\d{8})")

        if re.search(r"nota de cr[ée]dito", text, re.IGNORECASE):
            data["document_type"] = "07"
        elif re.search(r"nota de d[ée]bito", text, re.IGNORECASE):
            data["document_type"] = "08"
        elif re.search(r"boleta", text, re.IGNORECASE):
            data["document_type"] = "03"
        elif re.search(r"factura", text, re.IGNORECASE):
            data["document_type"] = "01"

        if m := re.search(r"\b([FBE][A-Z0-9]{3}|\d{4})\s*-\s*(\d{1,8})\b", text):
            data["series"], data["number"] = m[1], m[2]

        issued = find(r"fecha\s+de\s+emisi[oó]n\s*:?\s*([\d/.-]{8,10})")
        if issued:
            dmy = re.match(r"^(\d{2})[/.-](\d{2})[/.-](\d{4})$", issued)
            data["issue_date"] = f"{dmy[3]}-{dmy[2]}-{dmy[1]}" if dmy else issued

        if re.search(r"US\$|d[oó]lares|\bUSD\b", text, re.IGNORECASE):
            data["currency"] = "USD"
        elif re.search(r"S/|soles|\bPEN\b", text, re.IGNORECASE):
            data["currency"] = "PEN"

        data["supplier_name"] = next(
            (
                line
                for line in lines
                if not re.search(
                    r"ruc|factura|boleta|nota|fecha|electr[oó]nica", line, re.IGNORECASE
                )
            ),
            None,
        )
        data["customer_name"] = find(
            r"(?:se[ñn]or(?:\(es\))?|cliente|raz[oó]n social)\s*:\s*([^\n]+)"
        )

        operations = r"op(?:\.|eraci[oó]n(?:es)?)?\s*"
        data["taxable_amount"] = amount_after(operations + "gravadas?")
        data["exempt_amount"] = amount_after(operations + "exoneradas?")
        data["unaffected_amount"] = amount_after(operations + "inafectas?")
        data["subtotal"] = amount_after(r"sub\s*total")
        data["discount_amount"] = amount_after(r"descuentos?")
        data["igv"] = amount_after(r"\bI\.?G\.?V\.?(?:\s*\d+\s*%)?")
        data["icbper"] = amount_after("ICBPER")
        data["total"] = amount_after(
            r"(?:importe\s+)?total(?!\s*(?:de\s+)?descuentos?)(?:\s+a\s+pagar)?"
        )

        if re.search(r"sujeta\s+a\s+(?:la\s+)?detracci[oó]n", text, re.IGNORECASE):
            data["detraction_code"] = find(
                r"c[oó]digo\s+(?:de\s+)?(?:bien\s+o\s+servicio|detracci[oó]n)\s*:?\s*(\d{3})"
            )
            data["detraction_percent"] = find(
                r"detracci[oó]n\s*\(?\s*(\d{1,2}(?:[.,]\d{1,2})?)\s*%"
            )
            data["detraction_amount"] = find(
                r"monto\s+(?:de\s+)?(?:la\s+)?detracci[oó]n\s*:?\s*(?:S/\.?)?\s*([\d.,]+)"
            )
            data["detraction_account"] = find(r"banco\s+de\s+la\s+naci[oó]n\s*:?\s*([\d-]{6,})")

        for field_name in (
            "detraction_amount",
            "taxable_amount",
            "exempt_amount",
            "unaffected_amount",
            "subtotal",
            "discount_amount",
            "igv",
            "icbper",
            "total",
        ):
            value = data[field_name]
            if isinstance(value, str):
                data[field_name] = re.sub(r",(?=\d{3}\b)", "", value)

        parsed = Extraction.model_validate(data).model_dump()
        return AiExtractionResult(
            parsed, json.dumps(parsed, ensure_ascii=False), self.model, AiUsage()
        )

    def ping(self) -> str:
        return "proveedor simulado (sin IA)"
