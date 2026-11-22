"""Interpretación de la Consulta Integrada de Comprobante de Pago de SUNAT (manual v2.0).

Función pura: no consulta nada, solo traduce códigos a incidencias.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from invoicebot.domain.invoice import NewIssue, Severity

RECEIPT_STATUS = {
    "0": "NO EXISTE",
    "1": "ACEPTADO",
    "2": "ANULADO",
    "3": "AUTORIZADO",
    "4": "NO AUTORIZADO",
}

TAXPAYER_STATUS = {
    "00": "ACTIVO",
    "01": "BAJA PROVISIONAL",
    "02": "BAJA PROV. POR OFICIO",
    "03": "SUSPENSION TEMPORAL",
    "10": "BAJA DEFINITIVA",
    "11": "BAJA DE OFICIO",
    "22": "INHABILITADO-VENT.UNICA",
}

DOMICILE_CONDITION = {
    "00": "HABIDO",
    "09": "PENDIENTE",
    "11": "POR VERIFICAR",
    "12": "NO HABIDO",
    "20": "NO HALLADO",
}

#: Prefijo de las incidencias de SUNAT: se reemplazan en cada nueva consulta.
SUNAT_RULE_PREFIX = "SUNAT_"


@dataclass
class SunatResult:
    estado_cp: str | None
    estado_ruc: str | None
    cond_domi_ruc: str | None
    observaciones: list[str] = field(default_factory=list)
    raw: Any = None


def _issue(rule_code: str, severity: str, message: str, actual: str | None = None) -> NewIssue:
    return NewIssue(rule_code, severity, message, None, actual)


def evaluate_sunat_result(
    result: SunatResult, *, document_type: str | None
) -> tuple[str | None, list[NewIssue]]:
    issues: list[NewIssue] = []
    receipt_status = RECEIPT_STATUS.get(result.estado_cp) if result.estado_cp is not None else None

    match result.estado_cp:
        case "0":
            # Las boletas llegan en el resumen diario y pueden tardar unos días.
            boleta = document_type == "03"
            issues.append(
                _issue(
                    "SUNAT_RECEIPT_NOT_FOUND",
                    Severity.WARNING if boleta else Severity.ERROR,
                    "SUNAT todavía no tiene informada esta boleta (puede llegar en el resumen "
                    "diario); vuelve a consultar en unos días"
                    if boleta
                    else "SUNAT no tiene informado este comprobante: no debe registrarse hasta "
                    "confirmarlo con el proveedor",
                    receipt_status,
                )
            )
        case "2":
            issues.append(
                _issue(
                    "SUNAT_RECEIPT_VOIDED",
                    Severity.ERROR,
                    "El comprobante fue anulado (comunicado de baja ante SUNAT)",
                    receipt_status,
                )
            )
        case "4":
            issues.append(
                _issue(
                    "SUNAT_RECEIPT_NOT_AUTHORIZED",
                    Severity.ERROR,
                    "Comprobante físico no autorizado por imprenta",
                    receipt_status,
                )
            )
        case "1" | "3":
            pass
        case _:
            issues.append(
                _issue(
                    "SUNAT_RECEIPT_UNKNOWN_STATUS",
                    Severity.WARNING,
                    "SUNAT devolvió un estado de comprobante desconocido",
                    str(result.estado_cp),
                )
            )

    if result.estado_ruc and result.estado_ruc != "00":
        issues.append(
            _issue(
                "SUNAT_SUPPLIER_NOT_ACTIVE",
                Severity.WARNING,
                "El RUC del emisor no está ACTIVO en SUNAT",
                TAXPAYER_STATUS.get(result.estado_ruc, result.estado_ruc),
            )
        )
    if result.cond_domi_ruc in ("12", "20"):
        issues.append(
            _issue(
                "SUNAT_SUPPLIER_NOT_HABIDO",
                Severity.WARNING,
                "El emisor tiene domicilio NO HABIDO o NO HALLADO: revisa con tu contador el "
                "uso del crédito fiscal",
                DOMICILE_CONDITION.get(result.cond_domi_ruc),
            )
        )
    for note in result.observaciones or []:
        issues.append(_issue("SUNAT_OBSERVATION", Severity.INFO, str(note)[:500]))
    return receipt_status, issues
