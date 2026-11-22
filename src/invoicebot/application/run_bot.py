"""Un ciclo completo del bot desatendido (RPA).

1. Descarga los comprobantes adjuntos de los correos nuevos (si hay buzón).
2. Descarga el tipo de cambio del día y lo aplica a las facturas que lo esperaban.
3. Reintenta los documentos que fallaron por errores temporales.
4. Escanea la carpeta de entrada (y la del correo, si está fuera de ella).
5. Verifica en SUNAT, si está activado.
6. Exporta el mes actual y el anterior, si está configurado.
7. Hace una copia de seguridad si la última es más antigua que el intervalo.

Cada ciclo queda en bot_runs. Un fallo del correo, de SUNAT o de un documento no
detiene el ciclo: lo marca como parcial (código 3).
"""

from __future__ import annotations

import os
import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from invoicebot.application.deps import Deps
from invoicebot.application.exchange_rates import save_exchange_rates
from invoicebot.application.fetch_mail import MailSummary, fetch_mail
from invoicebot.application.ports import ExchangeRateSource, MailSource, SunatVerifier
from invoicebot.application.processing import ScanSummary, retry_documents, scan_invoices
from invoicebot.application.reports import export_invoices
from invoicebot.application.verify_sunat import SunatSummary, verify_sunat
from invoicebot.errors import ExitCode, InvoiceBotError, error_message


@dataclass
class BackupStep:
    is_due: Callable[[], bool]
    create: Callable[[], str]


@dataclass
class BotReport:
    inbox_dir: str
    mail: MailSummary | dict[str, str] | None = None
    retry: ScanSummary | None = None
    scan: ScanSummary | None = None
    sunat: SunatSummary | dict[str, str] | None = None
    fx: dict[str, Any] | None = None
    exports: list[dict[str, Any]] = field(default_factory=list)
    backup: str | None = None

    def compact(self) -> dict[str, Any]:
        """Lo esencial, para bot_runs (sin listas largas)."""
        s, sunat, mail, fx = self.scan, self.sunat, self.mail, self.fx or {}
        return {
            "found": s.found if s else 0,
            "extracted": s.extracted if s else 0,
            "valid": s.valid if s else 0,
            "warning": s.warning if s else 0,
            "error": s.error if s else 0,
            "failed": s.failed if s else 0,
            "needsOcr": s.needs_ocr if s else 0,
            "fromXml": s.from_xml if s else 0,
            "retried": self.retry.queued if self.retry else 0,
            "retryFailed": self.retry.failed if self.retry else 0,
            "sunatChecked": sunat.checked if isinstance(sunat, SunatSummary) else None,
            "sunatError": sunat.get("error") if isinstance(sunat, dict) else None,
            "mailFiles": mail.files if isinstance(mail, MailSummary) else None,
            "mailError": mail.get("error") if isinstance(mail, dict) else None,
            "fxRate": fx.get("sell"),
            "fxError": fx.get("error"),
            "exports": [e["path"] for e in self.exports],
            "backup": self.backup,
        }


@dataclass
class BotRunResult:
    run_id: int
    status: str
    exit_code: int
    report: BotReport
    error: str | None = None


def current_and_previous_month(today: str) -> tuple[str, str]:
    """ "2026-10-15" → ("2026-10", "2026-09"): las facturas de fin de mes llegan después."""
    year, month = int(today[:4]), int(today[5:7])
    previous = f"{year - 1}-12" if month == 1 else f"{year}-{month - 1:02d}"
    return today[:7], previous


def _is_inside(directory: str, parent: str) -> bool:
    try:
        Path(directory).resolve().relative_to(Path(parent).resolve())
    except ValueError:
        return False
    return True


def run_bot(
    deps: Deps,
    *,
    inbox_dir: str,
    recursive: bool = True,
    trigger: str = "manual",
    sunat_client: SunatVerifier | None = None,
    exchange_rates: ExchangeRateSource | None = None,
    mail: tuple[MailSource, str] | None = None,
    export_format: str = "none",
    backup: BackupStep | None = None,
) -> BotRunResult:
    repo, log = deps.repository, deps.logger
    run_id = repo.start_bot_run(trigger=trigger, host=socket.gethostname(), pid=os.getpid())
    log.info("inicio de ejecución #%s (%s)", run_id, trigger)
    report = BotReport(inbox_dir=inbox_dir)
    status, exit_code, error = "success", int(ExitCode.OK), None
    try:
        # Primero el correo: los adjuntos se escanean en este mismo ciclo.
        if mail:
            source, mail_dir = mail
            try:
                report.mail = fetch_mail(deps, source=source, directory=mail_dir)
            except Exception as mail_error:
                report.mail = {"error": error_message(mail_error)}
                log.error("no se pudo leer el correo: %s", report.mail["error"])
        # Antes de escanear: las facturas nuevas en dólares ya encuentran el tipo de cambio.
        if exchange_rates:
            try:
                today_rate = exchange_rates.fetch_today()
                applied = save_exchange_rates(deps, [today_rate])
                report.fx = {
                    "date": today_rate["date"],
                    "sell": today_rate["sell"],
                    "updated": applied["updated"],
                }
            except Exception as fx_error:
                report.fx = {"error": error_message(fx_error)}
                log.warning("tipo de cambio no disponible: %s", report.fx["error"])
        report.retry = retry_documents(deps, failed=True, pending=False)
        Path(inbox_dir).mkdir(parents=True, exist_ok=True)
        scan = scan_invoices(deps, root=inbox_dir, recursive=recursive)
        # Adjuntos guardados fuera de la carpeta de entrada (p. ej. si es de solo lectura).
        if mail and not (recursive and _is_inside(mail[1], inbox_dir)):
            Path(mail[1]).mkdir(parents=True, exist_ok=True)
            scan = scan.merge(scan_invoices(deps, root=mail[1], recursive=True))
        report.scan = scan
        if sunat_client:
            try:
                report.sunat = verify_sunat(deps, client=sunat_client)
            except Exception as sunat_error:
                report.sunat = {"error": error_message(sunat_error)}
        if export_format != "none":
            for month in current_and_previous_month(deps.today):
                result = export_invoices(deps, month=month, fmt=export_format)
                report.exports.append({"path": result.path, "rows": result.rows})
        if backup and backup.is_due():
            report.backup = backup.create()

        mail_problem = report.mail is not None and (
            isinstance(report.mail, dict) or report.mail.failed > 0
        )
        sunat_problem = report.sunat is not None and (
            isinstance(report.sunat, dict) or report.sunat.has_problems()
        )
        if scan.failed or scan.error or report.retry.failed or mail_problem or sunat_problem:
            status, exit_code = "partial", deps.config.run.partial_exit_code
    except Exception as cycle_error:
        status = "failed"
        exit_code = (
            cycle_error.exit_code
            if isinstance(cycle_error, InvoiceBotError)
            else int(ExitCode.GENERAL_ERROR)
        )
        error = error_message(cycle_error)
        log.exception("ejecución fallida: %s", error)
    finally:
        repo.finish_bot_run(
            run_id,
            status=status,
            exit_code=exit_code,
            summary=report.compact(),
            error_message=error,
        )
    log.info("fin de ejecución #%s: %s (código %s)", run_id, status, exit_code)
    return BotRunResult(run_id, status, exit_code, report, error)
