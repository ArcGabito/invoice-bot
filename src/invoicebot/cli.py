"""CLI de InvoiceBot (Typer). Mismos comandos y códigos de salida que la versión 0.x.

Códigos de salida: 0 completado · 1 error general · 2 argumentos o configuración ·
3 ejecución parcial (documentos fallidos o facturas con errores).
"""

from __future__ import annotations

import json
import sys
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

import typer

from invoicebot import __version__
from invoicebot.config import load_config
from invoicebot.context import AppContext
from invoicebot.errors import ConfigError, ExitCode, InvoiceBotError, LockedError, error_message
from invoicebot.money import format_cents

app = typer.Typer(
    name="invoicebot",
    help="Bot RPA de comprobantes de pago peruanos: XML UBL, PDF, OCR e IA, SUNAT y SQL Server.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode=None,
    pretty_exceptions_enable=False,
)
db_app = typer.Typer(help="Base de datos: init · migrate · status", no_args_is_help=True)
fx_app = typer.Typer(help="Tipo de cambio: set · fetch · import · list", no_args_is_help=True)
mail_app = typer.Typer(help="Correo: check · fetch · list · login", no_args_is_help=True)
user_app = typer.Typer(help="Usuarios de la pantalla de revisión", no_args_is_help=True)
key_app = typer.Typer(help="Claves de API para integrar otros sistemas", no_args_is_help=True)
jobs_app = typer.Typer(
    help="Trabajos en cola: list · show · retry · cancel · run", no_args_is_help=True
)
schedule_app = typer.Typer(
    help="Tareas programadas: list · tick · set · enable · disable", no_args_is_help=True
)
company_app = typer.Typer(help="Empresas (multiempresa): add · list · set", no_args_is_help=True)
app.add_typer(company_app, name="company")
purchasing_app = typer.Typer(
    help="Compras: import-orders · import-receipts · orders · match", no_args_is_help=True
)
approvals_app = typer.Typer(
    help="Aprobación de pago: list · approve · reject · rules · rule-add · delegate",
    no_args_is_help=True,
)
webhook_app = typer.Typer(
    help="Webhooks firmados: add · list · remove · events", no_args_is_help=True
)
app.add_typer(purchasing_app, name="purchasing")
app.add_typer(approvals_app, name="approvals")
app.add_typer(webhook_app, name="webhook")
sire_app = typer.Typer(help="SIRE (Registro de Compras): import · reconcile", no_args_is_help=True)
detraction_app = typer.Typer(
    help="Detracciones: pending · deposit · payment-file", no_args_is_help=True
)
taxpayer_app = typer.Typer(
    help="Condición tributaria de proveedores: set · import-padron · retentions",
    no_args_is_help=True,
)
app.add_typer(sire_app, name="sire")
app.add_typer(detraction_app, name="detractions")
app.add_typer(taxpayer_app, name="taxpayers")
accounting_app = typer.Typer(help="Contabilidad: entries · cost-center", no_args_is_help=True)
payments_app = typer.Typer(help="Pagos: payables · schedule · record", no_args_is_help=True)
app.add_typer(accounting_app, name="accounting")
app.add_typer(payments_app, name="payments")
app.add_typer(jobs_app, name="jobs")
app.add_typer(schedule_app, name="schedule")
app.add_typer(db_app, name="db")
app.add_typer(fx_app, name="fx")
app.add_typer(mail_app, name="mail")
app.add_typer(user_app, name="user")
app.add_typer(key_app, name="apikey")


@dataclass
class Options:
    provider: str | None = None
    data_dir: str | None = None
    concurrency: int | None = None
    json: bool = False
    verbose: bool = False
    user: str | None = None
    _context: AppContext | None = field(default=None, repr=False)

    def context(self) -> AppContext:
        if self._context is None:
            overrides = {
                "ai_provider": self.provider,
                "data_dir": self.data_dir,
                "concurrency": self.concurrency,
                "reviewer": self.user,
            }
            config = load_config(overrides={k: v for k, v in overrides.items() if v is not None})
            self._context = AppContext(config, verbose=self.verbose, reviewer=self.user)
        return self._context


OPTIONS = Options()


def _default(value: Any) -> Any:
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return value.hex()
    if hasattr(value, "as_dict"):
        return value.as_dict()
    if hasattr(value, "__dataclass_fields__"):
        from dataclasses import asdict

        return asdict(value)
    return str(value)


class Out:
    """Texto para personas o, con --json, un objeto para automatización."""

    def __init__(self, json_mode: bool):
        self.json_mode = json_mode
        self.payload: Any = None

    def line(self, text: str = "") -> None:
        if not self.json_mode:
            typer.echo(text)

    def data(self, payload: Any) -> None:
        self.payload = payload

    def flush(self) -> None:
        if self.json_mode and self.payload is not None:
            typer.echo(json.dumps(self.payload, ensure_ascii=False, indent=2, default=_default))


def _run(fn: Callable[[AppContext, Out], int | None]) -> None:
    out = Out(OPTIONS.json)
    code: int = ExitCode.OK
    try:
        code = fn(OPTIONS.context(), out) or ExitCode.OK
        out.flush()
    except InvoiceBotError as error:
        out.flush()
        typer.echo(f"Error: {error.message}", err=True)
        code = error.exit_code
    except KeyboardInterrupt:
        typer.echo("Interrumpido.", err=True)
        code = ExitCode.GENERAL_ERROR
    except Exception as error:
        if OPTIONS.verbose:
            raise
        typer.echo(
            f"Error inesperado: {error_message(error)} (usa --verbose para ver el detalle)",
            err=True,
        )
        code = ExitCode.GENERAL_ERROR
    finally:
        if OPTIONS._context is not None:
            OPTIONS._context.close()
            OPTIONS._context = None
    raise typer.Exit(int(code))


def _version(value: bool) -> None:
    if value:
        typer.echo(f"InvoiceBot {__version__}")
        raise typer.Exit()


@app.callback()
def main_options(
    provider: Annotated[str | None, typer.Option(help="anthropic | ollama | mock")] = None,
    data_dir: Annotated[str | None, typer.Option(help="Directorio de datos")] = None,
    concurrency: Annotated[
        int | None, typer.Option(min=1, max=16, help="Documentos a la vez")
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Salida JSON para automatización")
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
    user: Annotated[str | None, typer.Option(help="Quién firma las revisiones")] = None,
    version: Annotated[bool, typer.Option("--version", callback=_version, is_eager=True)] = False,
) -> None:
    OPTIONS.provider, OPTIONS.data_dir, OPTIONS.concurrency = provider, data_dir, concurrency
    OPTIONS.json, OPTIONS.verbose, OPTIONS.user = json_output, verbose, user
    OPTIONS._context = None


def _duration(ms: int) -> str:
    seconds = ms // 1000
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def _print_scan(out: Out, summary: Any, ctx: AppContext) -> None:
    out.line(f"Documentos encontrados: {summary.found}")
    out.line(f"Procesados correctamente: {summary.valid}")
    out.line(f"Con advertencias: {summary.warning}")
    out.line(f"Con errores: {summary.error}")
    out.line(f"Fallidos: {summary.failed}")
    out.line(f"Requieren OCR: {summary.needs_ocr}")
    out.line(
        f"Leídos del XML (sin IA): {summary.from_xml} · PDF vinculados a su XML: {summary.linked_to_xml}"
    )
    out.line(
        f"Ya procesados (omitidos): {summary.already_processed} · otros omitidos: {summary.skipped}"
    )
    out.line(
        f"Duplicados detectados: {summary.exact_duplicates + summary.possible_duplicates} "
        f"(exactos: {summary.exact_duplicates}, posibles: {summary.possible_duplicates})"
    )
    for failure in summary.failures[:20]:
        out.line(f"  ✗ {failure['file']}: {failure['message']}")
    if summary.unsupported:
        out.line(f"Archivos no compatibles ignorados: {len(summary.unsupported)}")
    out.line()
    out.line(f"Tiempo total: {_duration(summary.duration_ms)}")
    out.line(f"Base de datos: SQL Server {ctx.db.describe()}")


# --- Procesamiento -------------------------------------------------------------------


@app.command()
def scan(
    path: Annotated[str, typer.Argument(help="Carpeta o archivo")],
    recursive: Annotated[bool, typer.Option("--recursive", "-r")] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Inspecciona sin guardar ni llamar a la IA")
    ] = False,
    verify: Annotated[bool | None, typer.Option("--verify-sunat/--no-verify-sunat")] = None,
) -> None:
    """Procesa los documentos de una carpeta (o un archivo)."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.processing import scan_invoices
        from invoicebot.application.verify_sunat import verify_sunat

        should_verify = ctx.config.sunat.verify_on_scan if verify is None else verify
        sunat_client = ctx.sunat_client() if should_verify and not dry_run else None
        out.line(f"InvoiceBot v{__version__}")
        out.line()

        def progress(done: int, total: int, file_path: str, result: Any) -> None:
            if not out.json_mode:
                typer.echo(f"  [{done}/{total}] {Path(file_path).name}: {result.outcome}", err=True)

        summary = scan_invoices(
            ctx.deps, root=path, recursive=recursive, dry_run=dry_run, on_progress=progress
        )
        if dry_run:
            out.line(f"Simulación: {summary.found} documentos, {summary.queued} se procesarían.")
            out.data(summary)
            return ExitCode.OK
        _print_scan(out, summary, ctx)
        result: dict[str, Any] = {"scan": summary}
        code = ExitCode.PARTIAL if summary.failed or summary.error else ExitCode.OK
        if sunat_client:
            try:
                sunat = verify_sunat(ctx.deps, client=sunat_client)
                result["sunat"] = sunat
                out.line(f"SUNAT: {sunat.checked} consultados · {sunat.failed} fallidos")
                if sunat.has_problems():
                    code = ExitCode.PARTIAL
            except InvoiceBotError as error:
                out.line(f"La verificación en SUNAT falló: {error.message}")
                result["sunat"] = {"error": error.message}
                code = ExitCode.PARTIAL
        out.data(result)
        return code

    _run(command)


@app.command()
def retry(
    failed: Annotated[bool, typer.Option("--failed", help="Documentos fallidos")] = False,
    pending: Annotated[
        bool, typer.Option("--pending", help="Documentos de una ejecución interrumpida")
    ] = False,
    force: Annotated[bool, typer.Option("--force", help="También errores no recuperables")] = False,
) -> None:
    """Reintenta documentos fallidos o pendientes."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.processing import retry_documents

        both = not failed and not pending
        summary = retry_documents(
            ctx.deps, failed=failed or both, pending=pending or both, force=force
        )
        out.line(
            f"Candidatos: {summary.found} · reintentados: {summary.queued} · extraídos: {summary.extracted} · fallidos: {summary.failed}"
        )
        for skipped in summary.retry_skipped[:20]:
            out.line(f"  omitido {skipped['file']}: {skipped['reason']}")
        out.data(summary)
        return ExitCode.PARTIAL if summary.failed or summary.error else ExitCode.OK

    _run(command)


@app.command()
def run(
    watch: Annotated[
        bool, typer.Option("--watch", help="Repite el ciclo cada INVOICEBOT_RUN_INTERVAL_MINUTES")
    ] = False,
    trigger: Annotated[str, typer.Option(help="manual | scheduled | watch")] = "manual",
    inbox: Annotated[
        str | None, typer.Option(help="Carpeta de entrada (por defecto INVOICEBOT_INBOX_DIR)")
    ] = None,
) -> None:
    """Ciclo desatendido (RPA): correo, reintentos, escaneo, SUNAT, exportación y copia."""

    def one_cycle(ctx: AppContext, out: Out, kind: str) -> int:
        try:
            result = ctx.run_cycle(trigger=kind, inbox_dir=inbox)
        except LockedError as error:
            # Una tarea programada que coincide con un ciclo largo se omite sin error.
            out.line(f"Ciclo omitido: {error.message}")
            return ExitCode.OK
        compact = result.report.compact()
        out.line(
            f"Ejecución #{result.run_id}: {result.status} · encontrados {compact['found']} · extraídos "
            f"{compact['extracted']} · con errores {compact['error']} · fallidos {compact['failed']}"
        )
        if compact["mailFiles"] is not None or compact["mailError"]:
            out.line(
                f"Correo: {compact['mailFiles'] or 0} archivos{' · ' + compact['mailError'] if compact['mailError'] else ''}"
            )
        for path in compact["exports"]:
            out.line(f"Exportado: {path}")
        if compact["backup"]:
            out.line(f"Copia de seguridad: {compact['backup']}")
        if result.error:
            out.line(f"Error: {result.error}")
        out.data(
            {
                "run_id": result.run_id,
                "status": result.status,
                "exit_code": result.exit_code,
                "report": compact,
            }
        )
        return result.exit_code

    def command(ctx: AppContext, out: Out) -> int:
        if not watch:
            return one_cycle(ctx, out, trigger)
        interval = ctx.config.run.interval_minutes * 60
        out.line(
            f"Modo continuo: un ciclo cada {ctx.config.run.interval_minutes} minutos (Ctrl+C para salir)"
        )
        while True:
            started = time.monotonic()
            try:
                one_cycle(ctx, out, "watch")
            except Exception as error:  # Un corte de red o de SQL Server no detiene el bot.
                ctx.logger.error("error en el ciclo: %s", error_message(error))
                typer.echo(f"Error en el ciclo: {error_message(error)}", err=True)
            out.flush()
            time.sleep(max(5.0, interval - (time.monotonic() - started)))

    _run(command)


@app.command("verify-sunat")
def verify_sunat_command(
    month: Annotated[str | None, typer.Option(help="AAAA-MM")] = None,
    force: Annotated[
        bool, typer.Option(help="Vuelve a consultar también los ya verificados")
    ] = False,
    limit: Annotated[int | None, typer.Option(min=1)] = None,
) -> None:
    """Consulta en SUNAT si cada comprobante es válido y el estado del RUC emisor."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.reports import check_month
        from invoicebot.application.verify_sunat import verify_sunat

        check_month(month)
        summary = verify_sunat(
            ctx.review_deps(),
            client=ctx.sunat_client(),
            month=month,
            force=force,
            limit=limit,
            on_progress=lambda d, t, label, o: out.line(
                f"  [{d}/{t}] {label}: {o.get('receipt_status') or o.get('error')}"
            ),
        )
        out.line(
            f"Consultados: {summary.checked}/{summary.candidates} · aceptados {summary.accepted} · anulados "
            f"{summary.voided} · no existen {summary.not_found} · RUC no activo {summary.supplier_not_active} · "
            f"no habido {summary.supplier_not_habido} · fallidos {summary.failed}"
        )
        out.data(summary)
        return ExitCode.PARTIAL if summary.has_problems() else ExitCode.OK

    _run(command)


# --- Consultas -----------------------------------------------------------------------


@app.command()
def status() -> None:
    """Estado de la base de datos, documentos pendientes, consumo de tokens y últimas ejecuciones."""

    def command(ctx: AppContext, out: Out) -> int:
        summary = ctx.repository.get_status_summary()
        runs = ctx.repository.list_bot_runs(5)
        out.line(f"Base de datos: SQL Server {ctx.db.describe()}")
        out.line(f"Documentos: {summary['documents'] or 'ninguno'}")
        out.line(f"Facturas por validación: {summary['invoices'] or 'ninguna'}")
        out.line(f"Revisión: {summary['review'] or 'nada'}")
        out.line(
            f"Duplicados: exactos {summary['exact_duplicates']} · posibles {summary['possible_duplicates']}"
        )
        r = summary["runs"]
        out.line(
            f"Extracciones: {r['total']} ({r['failed']} fallidas) · tokens {r['input_tokens']} entrada / {r['output_tokens']} salida"
        )
        for item in runs:
            out.line(
                f"  #{item['id']} {item['started_at']:%Y-%m-%d %H:%M} UTC · {item['trigger']} · {item['status']}"
            )
        out.data({"summary": summary, "runs": runs})
        return ExitCode.OK

    _run(command)


@app.command()
def export(
    month: Annotated[str | None, typer.Option(help="AAAA-MM")] = None,
    csv_format: Annotated[bool, typer.Option("--csv")] = False,
    xlsx_format: Annotated[bool, typer.Option("--xlsx")] = False,
    status_filter: Annotated[
        list[str] | None, typer.Option("--status", help="valid | warning | error")
    ] = None,
    include_errors: Annotated[bool, typer.Option("--include-errors")] = False,
    include_duplicates: Annotated[bool, typer.Option("--include-duplicates")] = False,
    supplier: Annotated[str | None, typer.Option(help="RUC del emisor")] = None,
    currency: Annotated[str | None, typer.Option()] = None,
    output: Annotated[str | None, typer.Option("--output", "-o")] = None,
    delimiter: Annotated[str, typer.Option()] = ",",
    bom: Annotated[bool, typer.Option("--bom", help="BOM UTF-8 para Excel")] = False,
) -> None:
    """Exporta facturas a CSV o Excel."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.reports import export_invoices

        if csv_format and xlsx_format:
            raise ConfigError("Usa --csv o --xlsx, no ambos")
        fmt = "csv" if csv_format else "xlsx" if xlsx_format else ctx.config.export_format
        result = export_invoices(
            ctx.review_deps(),
            output=output,
            month=month,
            statuses=status_filter,
            include_errors=include_errors,
            include_duplicates=include_duplicates,
            supplier_ruc=supplier,
            currency=currency,
            fmt=fmt,
            delimiter=delimiter,
            bom=bom,
        )
        out.line(f"Exportadas {result.rows} facturas a {result.path}")
        out.data({"path": result.path, "rows": result.rows})
        return ExitCode.OK

    _run(command)


@app.command()
def duplicates() -> None:
    """Lista duplicados exactos y posibles coincidencias."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.reports import find_duplicates

        data = find_duplicates(ctx.review_deps())
        out.line(f"Duplicados exactos (mismo archivo en varias rutas): {len(data['exact'])}")
        for group in data["exact"]:
            out.line(
                f"  documento #{group['key']}: " + ", ".join(i["file_path"] for i in group["items"])
            )
        out.line(
            f"Posibles duplicados (mismo emisor, tipo, serie y número): {len(data['possible'])}"
        )
        for group in data["possible"]:
            out.line(
                f"  {group['key']}: "
                + ", ".join(f"#{i['invoice_id']} {i['file_path']}" for i in group["items"])
            )
        out.data(data)
        return ExitCode.OK

    _run(command)


@app.command()
def review() -> None:
    """Facturas con errores o advertencias, documentos fallidos y pendientes de OCR."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.review import get_review_queue

        queue = get_review_queue(ctx.review_deps())
        out.line(f"Facturas por revisar: {len(queue['invoices'])}")
        for item in queue["invoices"]:
            out.line(
                f"  #{item['invoice_id']} [{item['validation_status']}] {item['series']}-{item['number']} "
                f"{item['supplier_name'] or ''} · {item['currency'] or ''} {format_cents(item['total_cents'])}"
            )
            for issue in item["issues"]:
                out.line(f"      - {issue['severity']}: {issue['message']}")
        out.line(f"Documentos fallidos: {len(queue['failed'])}")
        for doc in queue["failed"]:
            out.line(f"  {doc['file_path']}: {doc['error_kind']} · {doc['error_message']}")
        out.line(f"Pendientes de OCR: {len(queue['needs_ocr'])}")
        out.data(queue)
        return ExitCode.OK

    _run(command)


@app.command()
def show(invoice_id: int) -> None:
    """Datos, incidencias e historial de una factura."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.review import get_invoice_detail
        from invoicebot.domain.invoice import AMOUNT_FIELDS, TEXT_FIELDS

        detail = get_invoice_detail(ctx.review_deps(), invoice_id)
        inv = detail["invoice"]
        out.line(
            f"Factura #{inv['id']} · {inv['file_name']} · {inv['validation_status']} · revisión {inv['review_status']}"
        )
        for name in TEXT_FIELDS:
            if inv.get(name) is not None:
                out.line(f"  {name}: {inv[name]}")
        for name in AMOUNT_FIELDS:
            if inv.get(f"{name}_cents") is not None:
                out.line(f"  {name}: {format_cents(inv[f'{name}_cents'])}")
        out.line("Incidencias:")
        for issue in detail["issues"]:
            state = " (resuelta)" if issue["resolved_at"] else ""
            out.line(
                f"  #{issue['id']} {issue['severity']} {issue['rule_code']}: {issue['message']}{state}"
            )
        if detail["mail_origins"]:
            out.line("Correo de origen:")
            for origin in detail["mail_origins"]:
                out.line(f"  {origin['sender']} · {origin['subject']} · {origin['received_at']}")
        out.line("Historial:")
        for action in detail["actions"]:
            out.line(
                f"  {action['created_at']:%Y-%m-%d %H:%M} {action['user']}: {action['action']} {action['field'] or ''} {action['note'] or ''}"
            )
        out.data(detail)
        return ExitCode.OK

    _run(command)


@app.command()
def approve(
    invoice_id: int,
    note: Annotated[str | None, typer.Option("--note", "-n")] = None,
    force: Annotated[bool, typer.Option(help="Aprobar aunque tenga errores (exige nota)")] = False,
) -> None:
    """Aprueba: confirma los datos y resuelve las advertencias."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.review import approve_invoice

        result = approve_invoice(
            ctx.review_deps(), invoice_id, user=ctx.reviewer, note=note, force=force
        )
        ctx.after_review_approved(int(result["invoice_id"]))
        out.line(f"Factura #{invoice_id} aprobada ({result['resolved']} incidencias resueltas)")
        out.data(result)
        return ExitCode.OK

    _run(command)


@app.command()
def reject(invoice_id: int, reason: Annotated[str, typer.Option("--reason", "-r")]) -> None:
    """Rechaza: deja de exportarse y de aparecer en la bandeja."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.review import reject_invoice

        out.data(reject_invoice(ctx.review_deps(), invoice_id, user=ctx.reviewer, reason=reason))
        ctx.after_review_rejected(invoice_id, reason)
        out.line(f"Factura #{invoice_id} rechazada")
        return ExitCode.OK

    _run(command)


@app.command()
def reopen(
    invoice_id: int, note: Annotated[str | None, typer.Option("--note", "-n")] = None
) -> None:
    """Devuelve una factura a pendiente."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.review import reopen_invoice

        out.data(reopen_invoice(ctx.review_deps(), invoice_id, user=ctx.reviewer, note=note))
        out.line(f"Factura #{invoice_id} reabierta")
        return ExitCode.OK

    _run(command)


@app.command()
def correct(
    invoice_id: int,
    changes: Annotated[
        list[str], typer.Argument(help="campo=valor (total=118.00 issue_date=15/10/2026)")
    ],
    note: Annotated[str | None, typer.Option("--note", "-n")] = None,
) -> None:
    """Corrige datos y revalida."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.review import correct_invoice

        result = correct_invoice(
            ctx.review_deps(), invoice_id, changes=changes, user=ctx.reviewer, note=note
        )
        for change in result["changed"]:
            out.line(
                f"  {change['field']}: {change['from'] or '(vacío)'} → {change['to'] or '(vacío)'}"
            )
        out.line(f"Estado de validación: {result['validation_status']}")
        out.data(result)
        return ExitCode.OK

    _run(command)


@app.command()
def resolve(
    issue_id: int, note: Annotated[str | None, typer.Option("--note", "-n")] = None
) -> None:
    """Da por resuelta una incidencia concreta."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.review import resolve_issue

        result = resolve_issue(ctx.review_deps(), issue_id, user=ctx.reviewer, note=note)
        out.line(
            f"Incidencia #{issue_id} resuelta · factura #{result['invoice_id']}: {result['validation_status']}"
        )
        out.data(result)
        return ExitCode.OK

    _run(command)


# --- Servidor ------------------------------------------------------------------------


@app.command()
def serve(
    host: Annotated[str | None, typer.Option(help="Por defecto INVOICEBOT_SERVE_HOST")] = None,
    port: Annotated[int | None, typer.Option(help="Por defecto INVOICEBOT_SERVE_PORT")] = None,
    open_browser: Annotated[bool, typer.Option("--open", help="Abre el navegador")] = False,
    workers: Annotated[int, typer.Option(min=1, max=32)] = 1,
) -> None:
    """Inicia la API REST y la pantalla de revisión (uvicorn)."""

    def command(ctx: AppContext, out: Out) -> int:
        import uvicorn

        config = ctx.config
        bind_host = host or config.serve.host
        bind_port = port or config.serve.port
        try:
            ctx.ensure_schema()
            if ctx.users.count_active() == 0:
                typer.echo(
                    "Aviso: no hay usuarios activos. Crea uno con `invoicebot user add <usuario>` "
                    "para entrar a la pantalla de revisión.",
                    err=True,
                )
        except Exception as error:
            # La API arranca igual (en un contenedor, SQL Server puede tardar): /readyz lo informa.
            typer.echo(
                f"Aviso: la base de datos aún no está lista: {error_message(error)}", err=True
            )
        tls = bool(config.serve.tls_cert and config.serve.tls_key)
        scheme = "https" if tls else "http"
        shown = "localhost" if bind_host in ("0.0.0.0", "::") else bind_host  # noqa: S104
        url = f"{scheme}://{shown}:{bind_port}/"
        typer.echo(f"InvoiceBot {__version__} escuchando en {url} (API en {url}docs)", err=True)
        if open_browser:
            webbrowser.open(url)
        ctx.close()
        uvicorn.run(
            "invoicebot.api.main:app",
            host=bind_host,
            port=bind_port,
            workers=workers,
            proxy_headers=True,
            forwarded_allow_ips=config.serve.forwarded_allow_ips,
            ssl_certfile=config.serve.tls_cert if tls else None,
            ssl_keyfile=config.serve.tls_key if tls else None,
            ssl_keyfile_password=config.secret("INVOICEBOT_SERVE_TLS_PASSPHRASE"),
            log_level="info" if OPTIONS.verbose else "warning",
            server_header=False,
        )
        return ExitCode.OK

    _run(command)


# --- Base de datos -------------------------------------------------------------------


@db_app.command("init")
def db_init() -> None:
    """Crea la base de datos y su inicio de sesión, y aplica las migraciones."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.infrastructure.db.admin import init_database

        result = init_database(ctx.config.db, ctx.config.env)
        out.line(
            f"Base de datos {ctx.config.db.name}: {'creada' if result.database_created else 'ya existía'}"
        )
        if result.login_created:
            out.line("Inicio de sesión de la aplicación creado")
        if result.user_created:
            out.line("Usuario de la base de datos creado (db_owner)")
        migration = ctx.ensure_schema()
        applied = migration.applied if migration else []
        out.line(f"Migraciones aplicadas: {', '.join(applied) if applied else 'ninguna (al día)'}")
        if ctx.config.env.get("INVOICEBOT_DB_ADMIN_PASSWORD"):
            out.line(
                "Ahora borra INVOICEBOT_DB_ADMIN_USER e INVOICEBOT_DB_ADMIN_PASSWORD del .env."
            )
        out.data({"init": result, "applied": applied})
        return ExitCode.OK

    _run(command)


@db_app.command("migrate")
def db_migrate() -> None:
    """Aplica las migraciones pendientes (con copia de seguridad previa)."""

    def command(ctx: AppContext, out: Out) -> int:
        migration = ctx.ensure_schema()
        applied = migration.applied if migration else []
        out.line(f"Migraciones aplicadas: {', '.join(applied) if applied else 'ninguna (al día)'}")
        if migration and migration.backup:
            out.line(f"Copia previa: {migration.backup}")
        out.data({"applied": applied, "backup": migration.backup if migration else None})
        return ExitCode.OK

    _run(command)


@db_app.command("status")
def db_status() -> None:
    """Versión del esquema."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.infrastructure.db.migration import schema_status

        info = schema_status(ctx.db)
        out.line(
            f"SQL Server {ctx.db.describe()} · esquema v{info['current']} de v{info['latest']}"
        )
        if info["pending"]:
            out.line(f"Pendientes: {', '.join(info['pending'])} (ejecuta `invoicebot db migrate`)")
        out.data(info)
        return ExitCode.OK

    _run(command)


@app.command()
def backup(
    list_only: Annotated[
        bool, typer.Option("--list", help="Muestra las copias existentes")
    ] = False,
) -> None:
    """Copia de seguridad verificada de la base de datos."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.infrastructure.db.backup import create_backup, list_backups

        if list_only:
            items = list_backups(ctx.db)
            for item in items:
                out.line(f"  {item.created_at:%Y-%m-%d %H:%M} UTC · {item.path}")
            if not items:
                out.line("No hay copias de InvoiceBot registradas en esta instancia.")
            out.data(items)
            return ExitCode.OK
        ctx.ensure_schema()
        result = create_backup(ctx.db, ctx.config.backup.dir, retention=ctx.config.backup.retention)
        out.line(f"Copia verificada: {result.path}")
        for name in result.removed:
            out.line(f"  borrada por retención: {name}")
        out.data(result)
        return ExitCode.OK

    _run(command)


# --- Tipo de cambio ------------------------------------------------------------------


@fx_app.command("set")
def fx_set(
    day: Annotated[str, typer.Argument(help="DD/MM/AAAA o AAAA-MM-DD")],
    sell: Annotated[float, typer.Argument(help="Venta")],
    buy: Annotated[float | None, typer.Option("--compra")] = None,
    currency: Annotated[str, typer.Option()] = "USD",
) -> None:
    """Registra el tipo de cambio de un día."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.exchange_rates import save_exchange_rates
        from invoicebot.infrastructure.sunat import parse_date

        iso = parse_date(day)
        if not iso or sell <= 0:
            raise ConfigError("Indica una fecha válida y un tipo de cambio mayor que cero")
        result = save_exchange_rates(
            ctx.review_deps(),
            [
                {
                    "currency": currency.upper(),
                    "date": iso,
                    "buy": buy,
                    "sell": sell,
                    "source": "manual",
                }
            ],
        )
        out.line(
            f"Tipo de cambio {currency.upper()} {iso}: {sell} · facturas actualizadas: {result['updated']}"
        )
        out.data(result)
        return ExitCode.OK

    _run(command)


@fx_app.command("fetch")
def fx_fetch() -> None:
    """Descarga el tipo de cambio de hoy publicado por SUNAT."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.exchange_rates import save_exchange_rates
        from invoicebot.infrastructure.sunat import SUNAT_EXCHANGE_RATE_URL, SunatExchangeRateSource

        source = SunatExchangeRateSource(
            url=ctx.config.fx.sunat_url or SUNAT_EXCHANGE_RATE_URL,
            timeout_s=ctx.config.fx.timeout_s,
        )
        rate = source.fetch_today()
        result = save_exchange_rates(ctx.review_deps(), [rate])
        out.line(
            f"SUNAT {rate['date']}: compra {rate['buy']} · venta {rate['sell']} · facturas actualizadas: {result['updated']}"
        )
        out.data({"rate": rate, **result})
        return ExitCode.OK

    _run(command)


@fx_app.command("import")
def fx_import(
    file: Annotated[Path, typer.Argument(exists=True, dir_okay=False)], currency: str = "USD"
) -> None:
    """Carga tipos de cambio desde un CSV (fecha, compra, venta)."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.exchange_rates import save_exchange_rates
        from invoicebot.infrastructure.sunat import parse_exchange_rate_csv

        rates, errors = parse_exchange_rate_csv(
            file.read_text(encoding="utf-8-sig"), currency=currency.upper()
        )
        for problem in errors:
            out.line(f"  {problem}")
        result = (
            save_exchange_rates(ctx.review_deps(), rates)
            if rates
            else {"saved": 0, "updated": 0, "missing": 0}
        )
        out.line(
            f"Tipos de cambio leídos: {len(rates)} · guardados: {result['saved']} · facturas actualizadas: {result['updated']}"
        )
        out.data({**result, "errors": errors})
        return ExitCode.PARTIAL if errors else ExitCode.OK

    _run(command)


@fx_app.command("list")
def fx_list(
    month: Annotated[str | None, typer.Option(help="AAAA-MM")] = None, currency: str = "USD"
) -> None:
    """Tipos de cambio registrados."""

    def command(ctx: AppContext, out: Out) -> int:
        from datetime import timedelta

        from invoicebot.application.reports import check_month
        from invoicebot.infrastructure.db.invoice_repository import month_range

        chosen = check_month(month) or ctx.config.today()[:7]
        start, following = month_range(chosen)
        items = ctx.repository.list_exchange_rates(
            currency=currency.upper(),
            start=start.isoformat(),
            end=(following - timedelta(days=1)).isoformat(),
        )
        for item in items:
            out.line(
                f"  {item['rate_date']} compra {item['buy']} venta {item['sell']} ({item['source']})"
            )
        if not items:
            out.line(f"Sin tipos de cambio de {currency.upper()} en {chosen}")
        out.data(items)
        return ExitCode.OK

    _run(command)


# --- Correo --------------------------------------------------------------------------


@mail_app.command("check")
def mail_check() -> None:
    """Prueba la conexión con el buzón y cuenta los correos con adjuntos recientes."""

    def command(ctx: AppContext, out: Out) -> int:
        from datetime import timedelta

        source = ctx.mail_source()
        address = source.connect()
        try:
            since = datetime.now(UTC) - timedelta(days=ctx.config.mail.since_days)
            refs = source.list_messages(since=since)
        finally:
            source.close()
        out.line(
            f"Conectado a {source.provider}:{address} · {len(refs)} correos con adjuntos en {ctx.config.mail.since_days} días"
        )
        out.data({"account": f"{source.provider}:{address}", "messages": len(refs)})
        return ExitCode.OK

    _run(command)


@mail_app.command("fetch")
def mail_fetch() -> None:
    """Descarga los adjuntos nuevos sin procesarlos."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.fetch_mail import fetch_mail

        summary = fetch_mail(ctx.review_deps(), source=ctx.mail_source(), directory=ctx.mail_dir())
        out.line(
            f"{summary.account}: leídos {summary.read} · con comprobantes {summary.saved} · archivos {summary.files} · "
            f"sin comprobantes {summary.no_attachments} · fallidos {summary.failed} · movidos {summary.moved}"
        )
        out.data(summary)
        return ExitCode.PARTIAL if summary.failed else ExitCode.OK

    _run(command)


@mail_app.command("list")
def mail_list(limit: int = 20) -> None:
    """Últimos correos leídos y qué se hizo con cada uno."""

    def command(ctx: AppContext, out: Out) -> int:
        items = ctx.repository.list_mail_messages(limit)
        for item in items:
            out.line(
                f"  {item['received_at']} · {item['sender']} · {item['subject']} · {item['status']} ({item['files']} archivos)"
            )
        out.data(items)
        return ExitCode.OK

    _run(command)


@mail_app.command("login")
def mail_login() -> None:
    """Inicia sesión en Gmail u Outlook / Microsoft 365 y guarda el acceso."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.infrastructure.mail.factory import login_scope
        from invoicebot.infrastructure.mail.login import (
            google_login,
            microsoft_device_login,
            write_mail_token,
        )

        mail = ctx.config.mail
        if mail.provider == "gmail":
            if not mail.google.client_id or not mail.google.client_secret_set:
                raise ConfigError(
                    "Define INVOICEBOT_MAIL_GOOGLE_CLIENT_ID e INVOICEBOT_MAIL_GOOGLE_CLIENT_SECRET"
                )
            token = google_login(
                client_id=mail.google.client_id,
                client_secret=ctx.config.secret("INVOICEBOT_MAIL_GOOGLE_CLIENT_SECRET") or "",
                scope=login_scope(mail),
                open_browser=webbrowser.open,
                say=typer.echo,
            )
            client_id = mail.google.client_id
        elif mail.provider == "graph":
            if not mail.microsoft.client_id:
                raise ConfigError("Define INVOICEBOT_MAIL_MS_CLIENT_ID")
            token = microsoft_device_login(
                client_id=mail.microsoft.client_id,
                tenant=mail.microsoft.tenant,
                scope=login_scope(mail),
                say=typer.echo,
            )
            client_id = mail.microsoft.client_id
        else:
            raise ConfigError("`mail login` es para INVOICEBOT_MAIL_PROVIDER=gmail o graph")
        write_mail_token(
            mail.token_file,
            provider=mail.provider,
            client_id=client_id,
            account=mail.user or "",
            refresh_token=token,
        )
        out.line(f"Acceso guardado en {mail.token_file} (protégelo como una contraseña)")
        return ExitCode.OK

    _run(command)


# --- Usuarios y claves de API --------------------------------------------------------


def _company_ids(ctx: AppContext, rucs: list[str] | None) -> list[int] | None:
    """--company RUC (repetible) → ids; sin --company, todas las empresas (None)."""
    if not rucs:
        return None
    from invoicebot.application.companies import require_company

    return [int(require_company(ctx.companies, ruc)["id"]) for ruc in rucs]


@user_app.command("add")
def user_add(
    username: str,
    name: Annotated[str | None, typer.Option("--name")] = None,
    password: Annotated[
        str | None, typer.Option(help="Sin indicarla se genera una aleatoria")
    ] = None,
    role: Annotated[
        str, typer.Option(help="admin | accountant | approver | treasury | auditor")
    ] = "admin",
    email: Annotated[
        str | None, typer.Option(help="Correo (para entrar con Microsoft o Google)")
    ] = None,
    company: Annotated[
        list[str] | None, typer.Option(help="RUC de una empresa que puede ver (repetible)")
    ] = None,
) -> None:
    """Da de alta a una persona en la pantalla de revisión."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.users import add_user

        result = add_user(
            ctx.users,
            username,
            display_name=name,
            password=password,
            role=role,
            email=email,
            company_ids=_company_ids(ctx, company),
        )
        out.line(f"Usuario {result.user['username']} creado con el rol {role}.")
        if result.generated:
            out.line(f"Contraseña (se muestra una sola vez): {result.password}")
        out.data({"user": result.user, "password": result.password if result.generated else None})
        return ExitCode.OK

    _run(command)


@user_app.command("list")
def user_list() -> None:
    def command(ctx: AppContext, out: Out) -> int:
        users = ctx.users.list_users()
        for u in users:
            out.line(
                f"  {u['username']} · {u['display_name']} · {u['role']} · {'activo' if u['active'] else 'desactivado'} · último acceso {u['last_login_at'] or 'nunca'}"
            )
        out.data(users)
        return ExitCode.OK

    _run(command)


@user_app.command("reset-password")
def user_reset(username: str, password: Annotated[str | None, typer.Option()] = None) -> None:
    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.users import reset_password

        result = reset_password(ctx.users, username, password=password)
        out.line(f"Contraseña de {result.user['username']} cambiada; sus sesiones se cerraron.")
        if result.generated:
            out.line(f"Nueva contraseña (se muestra una sola vez): {result.password}")
        return ExitCode.OK

    _run(command)


@user_app.command("disable")
def user_disable(username: str) -> None:
    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.users import set_user_active

        set_user_active(ctx.users, username, active=False)
        out.line(f"Usuario {username} desactivado; sus sesiones se cerraron.")
        return ExitCode.OK

    _run(command)


@user_app.command("enable")
def user_enable(username: str) -> None:
    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.users import set_user_active

        set_user_active(ctx.users, username, active=True)
        out.line(f"Usuario {username} activado.")
        return ExitCode.OK

    _run(command)


@user_app.command("set-role")
def user_set_role(
    username: str,
    role: Annotated[str, typer.Argument(help="admin | accountant | approver | treasury | auditor")],
) -> None:
    """Cambia el rol de un usuario."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.users import set_role

        user = set_role(ctx.users, username, role)
        out.line(f"{user['username']}: rol {user['role']}")
        out.data(user)
        return ExitCode.OK

    _run(command)


@user_app.command("companies")
def user_companies(
    username: str,
    company: Annotated[list[str] | None, typer.Option(help="RUC (repetible)")] = None,
    all_companies: Annotated[bool, typer.Option("--all", help="Todas las empresas")] = False,
) -> None:
    """Limita a un usuario a ciertas empresas (o le da todas con --all)."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.users import set_companies

        if all_companies == bool(company):
            raise ConfigError("Usa --company RUC (una o más veces) o --all")
        user = set_companies(
            ctx.users, username, None if all_companies else _company_ids(ctx, company)
        )
        scope = "todas" if user["all_companies"] else ", ".join(map(str, user["company_ids"]))
        out.line(f"{user['username']}: empresas {scope}")
        out.data(user)
        return ExitCode.OK

    _run(command)


@user_app.command("email")
def user_email(username: str, email: str) -> None:
    """Asocia un correo para entrar con Microsoft o Google."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.users import _require

        user = _require(ctx.users, username)
        ctx.users.set_email(user["id"], email)
        out.line(f"{user['username']}: correo {email}")
        out.data({"username": user["username"], "email": email})
        return ExitCode.OK

    _run(command)


@key_app.command("create")
def apikey_create(
    name: Annotated[str, typer.Argument(help="Sistema que la usará (p. ej. email-bot)")],
    scope: Annotated[str, typer.Option(help="read | write")] = "write",
    company: Annotated[str | None, typer.Option(help="RUC: la clave solo ve esa empresa")] = None,
    permissions: Annotated[
        str | None,
        typer.Option(
            help="Permisos finos separados por comas (p. ej. documents:write,invoices:read)"
        ),
    ] = None,
) -> None:
    """Crea una clave de API (se muestra una sola vez)."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.users import create_api_key

        ids = _company_ids(ctx, [company] if company else None)
        key, row = create_api_key(
            ctx.users,
            name,
            scope=scope,
            created_by=ctx.reviewer,
            company_id=ids[0] if ids else None,
            permissions=permissions,
        )
        out.line(f"Clave {row['prefix']} ({scope}) para {row['name']}:")
        out.line(key)
        out.line(
            "Guárdala en el gestor de secretos del sistema que la usará: no se vuelve a mostrar."
        )
        out.data({"key": key, **row})
        return ExitCode.OK

    _run(command)


@key_app.command("list")
def apikey_list() -> None:
    def command(ctx: AppContext, out: Out) -> int:
        keys = ctx.users.list_api_keys()
        for k in keys:
            out.line(
                f"  #{k['id']} {k['prefix']} · {k['name']} · {k['scope']} · {'activa' if k['active'] else 'revocada'} · último uso {k['last_used_at'] or 'nunca'}"
            )
        out.data(keys)
        return ExitCode.OK

    _run(command)


@key_app.command("revoke")
def apikey_revoke(
    key: Annotated[str, typer.Argument(help="Prefijo (ibk_xxxxxxxx) o número")],
) -> None:
    def command(ctx: AppContext, out: Out) -> int:
        if not ctx.users.revoke_api_key(key):
            raise ConfigError(f"No hay una clave activa {key}")
        out.line(f"Clave {key} revocada.")
        return ExitCode.OK

    _run(command)


# --- Diagnóstico ---------------------------------------------------------------------


@app.command()
def doctor() -> None:
    """Comprueba Python, SQL Server, lectura de PDF, OCR, IA, SUNAT y correo."""

    def command(ctx: AppContext, out: Out) -> int:
        checks: list[dict[str, Any]] = []

        def check(name: str, fn: Callable[[], str], *, optional: bool = False) -> None:
            try:
                detail, ok = fn(), True
            except Exception as error:
                detail, ok = error_message(error), False
            checks.append({"name": name, "ok": ok, "optional": optional, "detail": detail})
            out.line(f"{'✓' if ok else ('·' if optional else '✗')} {name}: {detail}")

        check("Python", lambda: sys.version.split()[0])
        check("SQL Server", ctx.db.ping)
        check(f"Cola ({ctx.config.queue.backend})", ctx.queue.ping)

        def schema() -> str:
            from invoicebot.infrastructure.db.migration import schema_status

            info = schema_status(ctx.db)
            if info["pending"]:
                raise ConfigError(f"faltan migraciones: {', '.join(info['pending'])}")
            return f"esquema v{info['current']}"

        check("Esquema", schema)

        def pdf() -> str:
            import pypdfium2

            return f"PDFium {pypdfium2.PDFIUM_INFO.version if hasattr(pypdfium2, 'PDFIUM_INFO') else 'disponible'}"

        check("Lectura de PDF", pdf)
        engine = ctx.config.ocr.engine
        if engine == "tesseract":
            from invoicebot.infrastructure.ocr import create_ocr_engine

            ocr = create_ocr_engine(ctx.config)
            check("OCR (Tesseract)", lambda: ocr.ping() if ocr else "desactivado")
        else:
            check("OCR", lambda: f"motor {engine}", optional=True)
        check(f"IA ({ctx.config.ai_provider})", ctx.ai_provider.ping)
        if ctx.config.sunat.enabled:
            check("SUNAT", lambda: ctx.sunat_client().ping())
        else:
            check("SUNAT", lambda: "desactivada (INVOICEBOT_SUNAT_ENABLED=false)", optional=True)
        if ctx.config.mail.provider != "none":

            def mail() -> str:
                source = ctx.mail_source()
                try:
                    return f"{source.provider}:{source.connect()}"
                finally:
                    source.close()

            check("Correo", mail)
        out.data(checks)
        return (
            ExitCode.OK if all(c["ok"] or c["optional"] for c in checks) else ExitCode.GENERAL_ERROR
        )

    _run(command)


@app.command("config")
def show_config() -> None:
    """Configuración efectiva, sin revelar secretos."""

    def command(ctx: AppContext, out: Out) -> int:
        c = ctx.config
        out.line(f"Proveedor de IA:     {c.ai_provider}")
        if c.ai_provider == "anthropic":
            out.line(f"  Modelo:            {c.anthropic.model} (effort {c.anthropic.effort})")
            out.line(
                f"  ANTHROPIC_API_KEY: {'definida' if c.anthropic.api_key_set else 'no definida'}"
            )
        if c.ai_provider == "ollama":
            out.line(f"  Ollama:            {c.ollama.url} · {c.ollama.model}")
        out.line(
            f"Base de datos:       SQL Server {ctx.db.describe()} · cifrado {'sí' if c.db.encrypt else 'no'}"
        )
        out.line(f"OCR:                 {c.ocr.engine} ({', '.join(c.ocr.languages)})")
        out.line(f"SUNAT:               {'activada' if c.sunat.enabled else 'desactivada'}")
        out.line(
            f"Correo:              {c.mail.provider}{' · ' + (c.mail.user or '') if c.mail.provider != 'none' else ''}"
        )
        out.line(f"Carpeta de entrada:  {c.run.inbox_dir or 'no definida'}")
        out.line(
            f"Exportaciones:       {c.export_dir} ({c.export_format}) · en cada ciclo: {c.run.export}"
        )
        out.line(f"Datos:               {c.data_dir}")
        out.line(
            f"API:                 {c.serve.host}:{c.serve.port} · docs {'sí' if c.serve.api_docs else 'no'}"
        )
        out.line(f"Zona horaria:        {c.timezone} (hoy {c.today()})")
        out.line(
            f"Cola:                {c.queue.backend}"
            + (
                f" · Redis {'definido' if c.queue.redis_url_set else 'no definido'}"
                if c.queue.backend == "redis"
                else ""
            )
            + f" · espacio {c.queue.namespace}"
        )
        out.data(
            {
                "ai_provider": c.ai_provider,
                "anthropic": {
                    "model": c.anthropic.model,
                    "effort": c.anthropic.effort,
                    "api_key": c.anthropic.api_key_set,
                },
                "database": ctx.db.describe(),
                "ocr": c.ocr.engine,
                "sunat": c.sunat.enabled,
                "mail": c.mail.provider,
                "inbox_dir": c.run.inbox_dir,
                "export_dir": c.export_dir,
                "timezone": c.timezone,
                "queue": {"backend": c.queue.backend, "namespace": c.queue.namespace},
            }
        )
        return ExitCode.OK

    _run(command)


# --- Empresas y auditoría ------------------------------------------------------------------


@company_app.command("add")
def company_add(
    ruc: str,
    name: str,
    retention_agent: Annotated[
        bool, typer.Option("--retention-agent", help="Agente de retención del IGV")
    ] = False,
    good_taxpayer: Annotated[
        bool, typer.Option("--good-taxpayer", help="Buen contribuyente")
    ] = False,
) -> None:
    """Registra una empresa (RUC adquirente) y le asigna sus comprobantes ya guardados."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.companies import add_company

        company = add_company(
            ctx.companies,
            ruc=ruc,
            name=name,
            retention_agent=retention_agent,
            good_taxpayer=good_taxpayer,
        )
        out.line(
            f"Empresa {company['ruc']} · {company['name']} creada "
            f"({company['assigned_invoices']} comprobantes asignados)."
        )
        out.data(company)
        return ExitCode.OK

    _run(command)


@company_app.command("list")
def company_list() -> None:
    """Empresas registradas."""

    def command(ctx: AppContext, out: Out) -> int:
        rows = ctx.companies.list_companies()
        for c in rows:
            flags = [
                f
                for f, on in (
                    ("agente de retención", c["retention_agent"]),
                    ("buen contribuyente", c["good_taxpayer"]),
                )
                if on
            ]
            state = "activa" if c["active"] else "inactiva"
            out.line(
                f"  {c['ruc']} · {c['name']} · {state}{' · ' + ', '.join(flags) if flags else ''}"
            )
        if not rows:
            out.line("Sin empresas: InvoiceBot funciona como una sola empresa.")
        out.data(rows)
        return ExitCode.OK

    _run(command)


@company_app.command("set")
def company_set(
    ruc: str,
    name: Annotated[str | None, typer.Option()] = None,
    retention_agent: Annotated[
        bool | None, typer.Option("--retention-agent/--no-retention-agent")
    ] = None,
    good_taxpayer: Annotated[
        bool | None, typer.Option("--good-taxpayer/--no-good-taxpayer")
    ] = None,
    active: Annotated[bool | None, typer.Option("--active/--inactive")] = None,
) -> None:
    """Cambia los datos de una empresa (el historial queda en companies_history)."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.companies import require_company

        company = require_company(ctx.companies, ruc)
        ctx.companies.update(
            int(company["id"]),
            name=name,
            retention_agent=retention_agent,
            good_taxpayer=good_taxpayer,
            active=active,
        )
        updated = ctx.companies.get(int(company["id"]))
        out.line(f"Empresa {ruc} actualizada.")
        out.data(updated)
        return ExitCode.OK

    _run(command)


@app.command()
def history(invoice_id: int) -> None:
    """Historial de cambios de una factura: cada estado, cuándo y quién lo hizo."""

    def command(ctx: AppContext, out: Out) -> int:
        versions = ctx.repository.invoice_history(invoice_id)
        if not versions:
            raise ConfigError(f"No existe la factura #{invoice_id}")
        labels = {"I": "alta", "U": "cambio", "D": "baja"}
        for v in versions:
            reviewer = f" ({v['reviewed_by']})" if v["reviewed_by"] else ""
            out.line(
                f"  {v['changed_at']:%Y-%m-%d %H:%M:%S} {labels.get(v['operation'], v['operation'])} "
                f"por {v['changed_by'] or '—'}: validación {v['validation_status']}, "
                f"revisión {v['review_status']}{reviewer}, SUNAT {v['sunat_status'] or '—'}"
            )
        out.data({"invoice_id": invoice_id, "versions": versions})
        return ExitCode.OK

    _run(command)


# --- Compras, aprobaciones y webhooks ------------------------------------------------------


@purchasing_app.command("import-orders")
def purchasing_import_orders(path: Path) -> None:
    """Órdenes de compra desde CSV (una fila por línea; importes sin IGV)."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.purchasing import import_orders_csv

        result = import_orders_csv(ctx, path)
        out.line(
            f"{result['orders']} órdenes ({result['created']} nuevas, {result['updated']} actualizadas)"
        )
        out.data(result)
        return ExitCode.OK

    _run(command)


@purchasing_app.command("import-receipts")
def purchasing_import_receipts(path: Path) -> None:
    """Recepciones (notas de ingreso, guías, conformidades) desde CSV."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.purchasing import import_receipts_csv

        result = import_receipts_csv(ctx, path)
        out.line(f"{result['receipts']} recepciones registradas")
        out.data(result)
        return ExitCode.OK

    _run(command)


@purchasing_app.command("orders")
def purchasing_orders(
    supplier: Annotated[str | None, typer.Option(help="RUC del proveedor")] = None,
    status: Annotated[str | None, typer.Option(help="open | closed | cancelled")] = None,
) -> None:
    """Órdenes de compra con lo recibido y lo facturado."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.purchasing import list_orders

        rows = list_orders(ctx, company_ids=None, supplier_ruc=supplier, status=status)
        for o in rows:
            out.line(
                f"  {o['po_number']:<14} {o['supplier_ruc']} {o['currency']} "
                f"pedido {format_cents(o['total_cents'])} · recibido {format_cents(o['received_cents'])} "
                f"· facturado {format_cents(o['invoiced_cents'])} · {o['status']}"
            )
        out.data(rows)
        return ExitCode.OK

    _run(command)


@purchasing_app.command("match")
def purchasing_match(
    invoice_id: int, po: Annotated[str | None, typer.Option(help="Vincular a esta orden")] = None
) -> None:
    """Hace (o rehace) el match de una factura con su orden de compra."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.purchasing import run_match

        result = run_match(ctx, invoice_id, po_number=po)
        out.line(
            f"Factura #{invoice_id}: {result.get('status', 'nota')} {result.get('po_number') or ''}"
        )
        for e in result.get("exceptions") or []:
            out.line(
                f"  · {e['code']}: {e['message']} (esperado {e['expected']}, recibido {e['actual']})"
            )
        out.data(result)
        return ExitCode.OK

    _run(command)


@approvals_app.command("list")
def approvals_list(
    status: Annotated[
        str, typer.Option(help="pending | approved | rejected | escalated")
    ] = "pending",
    approver: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Aprobaciones de pago (por defecto, las pendientes)."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.approvals import list_requests

        rows = list_requests(ctx, status=status, approver=approver)
        for r in rows:
            late = " · VENCIDA" if r["overdue"] else ""
            out.line(
                f"  #{r['id']:<6} {r['invoice']:<16} {r['currency']} {format_cents(r['total_cents'])} "
                f"nivel {r['level']} → {r['approver']} · vence {r['due_at']:%Y-%m-%d %H:%M}{late}"
            )
        out.data(rows)
        return ExitCode.OK

    _run(command)


def _decide(approval_id: int, approve: bool, comment: str | None) -> None:
    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.approvals import decide

        result = decide(
            ctx, approval_id, approve=approve, user=ctx.reviewer, comment=comment, is_admin=True
        )
        out.line(f"Aprobación #{approval_id}: factura {result['approval_status']}")
        out.data(result)
        return ExitCode.OK

    _run(command)


@approvals_app.command("approve")
def approvals_approve(
    approval_id: int, comment: Annotated[str | None, typer.Option("--comment", "-c")] = None
) -> None:
    """Aprueba el pago (como administrador desde la CLI)."""
    _decide(approval_id, True, comment)


@approvals_app.command("reject")
def approvals_reject(
    approval_id: int, comment: Annotated[str, typer.Option("--comment", "-c")]
) -> None:
    """Rechaza el pago (exige motivo)."""
    _decide(approval_id, False, comment)


@approvals_app.command("rules")
def approvals_rules() -> None:
    """Matriz de aprobación."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.approvals import list_rules

        rows = list_rules(ctx)
        for r in rows:
            upper = (
                format_cents(r["max_amount_cents"]) if r["max_amount_cents"] is not None else "∞"
            )
            scope = ", ".join(
                f"{k} {r[k]}"
                for k in ("company_id", "cost_center", "supplier_ruc")
                if r[k] is not None
            )
            out.line(
                f"  #{r['id']:<4} nivel {r['level']} · S/ {format_cents(r['min_amount_cents'])} a {upper} → "
                f"{r['approver'] or 'automático'} (SLA {r['sla_hours']} h"
                f"{', escala a ' + r['escalate_to'] if r['escalate_to'] else ''}){' · ' + scope if scope else ''}"
                f"{'' if r['active'] else ' · inactiva'}"
            )
        out.data(rows)
        return ExitCode.OK

    _run(command)


@approvals_app.command("rule-add")
def approvals_rule_add(
    level: Annotated[int, typer.Option(min=1, max=9)] = 1,
    approver: Annotated[
        str | None, typer.Option(help="Usuario; vacío = automático si hay match")
    ] = None,
    min_amount: Annotated[float, typer.Option("--min", help="Soles (incluido)")] = 0,
    max_amount: Annotated[float | None, typer.Option("--max", help="Soles (excluido)")] = None,
    company: Annotated[str | None, typer.Option(help="RUC de la empresa")] = None,
    cost_center: Annotated[str | None, typer.Option()] = None,
    supplier: Annotated[str | None, typer.Option(help="RUC del proveedor")] = None,
    sla_hours: Annotated[int, typer.Option(min=1)] = 48,
    escalate_to: Annotated[
        str | None, typer.Option(help="Usuario al que se escala si vence")
    ] = None,
) -> None:
    """Agrega una regla a la matriz de aprobación."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.approvals import add_rule

        rule = add_rule(
            ctx,
            level=level,
            approver=approver,
            min_amount_cents=round(min_amount * 100),
            max_amount_cents=round(max_amount * 100) if max_amount is not None else None,
            company_id=_company_ids(ctx, [company])[0] if company else None,  # type: ignore[index]
            cost_center=cost_center,
            supplier_ruc=supplier,
            sla_hours=sla_hours,
            escalate_to=escalate_to,
        )
        out.line(f"Regla #{rule['id']} creada")
        out.data(rule)
        return ExitCode.OK

    _run(command)


@approvals_app.command("rule-remove")
def approvals_rule_remove(rule_id: int) -> None:
    """Desactiva una regla (se conserva para el historial)."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.approvals import disable_rule

        if not disable_rule(ctx, rule_id):
            raise ConfigError(f"No existe la regla #{rule_id}")
        out.line(f"Regla #{rule_id} desactivada")
        return ExitCode.OK

    _run(command)


@approvals_app.command("delegate")
def approvals_delegate(
    username: str,
    to: Annotated[str, typer.Option("--to", help="Quién aprueba en su lugar")],
    starts: Annotated[str, typer.Option("--from", help="AAAA-MM-DD")],
    ends: Annotated[str, typer.Option("--until", help="AAAA-MM-DD (incluido)")],
    reason: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Delega las aprobaciones de alguien durante unas fechas (vacaciones, licencias)."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.approvals import delegate

        try:
            start_day, end_day = date.fromisoformat(starts), date.fromisoformat(ends)
        except ValueError as error:
            raise ConfigError("Usa fechas AAAA-MM-DD") from error
        row = delegate(
            ctx, username=username, to=to, starts_on=start_day, ends_on=end_day, reason=reason
        )
        out.line(f"{username} delega en {to} del {starts} al {ends}")
        out.data(row)
        return ExitCode.OK

    _run(command)


@webhook_app.command("add")
def webhook_add(
    name: str,
    url: str,
    events: Annotated[
        str, typer.Option(help='Eventos separados por comas; "*" o "invoice.*"')
    ] = "*",
    company: Annotated[str | None, typer.Option(help="RUC: solo eventos de esa empresa")] = None,
) -> None:
    """Suscribe una URL a eventos (firma HMAC-SHA256; el secreto se muestra una sola vez)."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.integrations import add_webhook

        hook = add_webhook(ctx, name=name, url=url, events=events, company_ruc=company)
        out.line(f"Webhook #{hook['id']} {hook['name']} → {hook['url']} ({hook['events']})")
        out.line(f"Secreto de firma (se muestra una sola vez): {hook['secret']}")
        out.data(hook)
        return ExitCode.OK

    _run(command)


@webhook_app.command("list")
def webhook_list() -> None:
    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.integrations import list_webhooks

        rows = list_webhooks(ctx)
        for h in rows:
            state = "activo" if h["active"] else "inactivo"
            out.line(
                f"  #{h['id']} {h['name']} → {h['url']} · {h['events']} · {state} · {h['failed_deliveries']} entregas fallidas"
            )
        out.data(rows)
        return ExitCode.OK

    _run(command)


@webhook_app.command("remove")
def webhook_remove(webhook_id: int) -> None:
    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.integrations import disable_webhook

        if not disable_webhook(ctx, webhook_id):
            raise ConfigError(f"No existe el webhook #{webhook_id}")
        out.line(f"Webhook #{webhook_id} desactivado")
        return ExitCode.OK

    _run(command)


@webhook_app.command("events")
def webhook_events(
    event_type: Annotated[str | None, typer.Option("--type")] = None,
    limit: Annotated[int, typer.Option(min=1, max=500)] = 30,
) -> None:
    """Últimos eventos publicados y el estado de sus entregas."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.integrations import list_events

        rows = list_events(ctx, event_type=event_type, limit=limit)
        for e in rows:
            states = ", ".join(d["status"] for d in e["deliveries"]) or "sin suscriptores"
            out.line(
                f"  {e['time']:%Y-%m-%d %H:%M:%S} {e['type']:<30} {e['subject'] or ''} · {states}"
            )
        out.data(rows)
        return ExitCode.OK

    _run(command)


# --- Cumplimiento tributario ---------------------------------------------------------------


@sire_app.command("import")
def sire_import(
    path: Path,
    period: Annotated[str, typer.Option(help="AAAA-MM")],
    company: Annotated[str | None, typer.Option(help="RUC de la empresa")] = None,
) -> None:
    """Importa la propuesta del Registro de Compras (RCE) descargada del SIRE."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.tax import import_sire_proposal

        result = import_sire_proposal(
            ctx, path, period=period, company_ruc=company, created_by=ctx.reviewer
        )
        out.line(f"Propuesta {result['period']}: {result['rows']} comprobantes importados")
        out.data(result)
        return ExitCode.OK

    _run(command)


@sire_app.command("reconcile")
def sire_reconcile(
    period: Annotated[str, typer.Option(help="AAAA-MM")],
    company: Annotated[str | None, typer.Option(help="RUC de la empresa")] = None,
    xlsx: Annotated[Path | None, typer.Option(help="Guarda las diferencias en Excel")] = None,
) -> None:
    """Cruza la propuesta del SIRE con los comprobantes registrados."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.tax import reconcile, reconciliation_xlsx

        data = reconcile(ctx, period=period, company_ruc=company)
        s = data["summary"]
        out.line(
            f"Periodo {data['period']}: {s['matched']} coinciden · {s['only_sunat']} solo en SUNAT · "
            f"{s['only_bot']} solo en InvoiceBot · {s['amount'] + s['igv'] + s['date']} con datos distintos"
        )
        for d in data["differences"][:50]:
            out.line(
                f"  {d['kind']:<10} {d['supplier_ruc']} {d['document_type']} {d['series']}-{d['number']}: {d['message']}"
            )
        if xlsx:
            xlsx.write_bytes(reconciliation_xlsx(data))
            out.line(f"Excel: {xlsx}")
        out.data(data)
        return ExitCode.PARTIAL if data["differences"] else ExitCode.OK

    _run(command)


@detraction_app.command("pending")
def detractions_pending() -> None:
    """Facturas sujetas a detracción sin constancia de depósito."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.tax import pending_detractions

        rows = pending_detractions(ctx)
        for r in rows:
            out.line(
                f"  #{r['invoice_id']:<6} {r['invoice']:<16} {r['supplier_ruc']} código {r['detraction_code']} "
                f"S/ {format_cents(r['deposit_cents'])} · cuenta {r['account'] or '—'} · {r['approval_status'] or 'sin aprobar'}"
            )
        out.data(rows)
        return ExitCode.OK

    _run(command)


@detraction_app.command("deposit")
def detractions_deposit(
    invoice_id: int,
    constancia: Annotated[str, typer.Option(help="Número de la constancia de depósito")],
    deposit_date: Annotated[str, typer.Option("--date", help="AAAA-MM-DD")],
    amount: Annotated[float, typer.Option(help="Soles depositados")],
) -> None:
    """Registra la constancia de depósito de una detracción."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.tax import record_deposit

        try:
            day = date.fromisoformat(deposit_date)
        except ValueError as error:
            raise ConfigError("Usa la fecha AAAA-MM-DD") from error
        row = record_deposit(
            ctx,
            invoice_id=invoice_id,
            constancia=constancia,
            deposit_date=day,
            amount_cents=round(amount * 100),
            created_by=ctx.reviewer,
        )
        out.line(f"Constancia {row['constancia']} registrada para la factura #{invoice_id}")
        out.data(row)
        return ExitCode.OK

    _run(command)


@detraction_app.command("payment-file")
def detractions_payment_file(
    invoice_ids: Annotated[list[int], typer.Argument(help="Facturas a depositar")],
    lot: Annotated[str, typer.Option(help="Lote AANNNN, p. ej. 260001")],
    output: Annotated[Path, typer.Option("--output", "-o")],
    company: Annotated[str | None, typer.Option(help="RUC de la empresa")] = None,
) -> None:
    """Genera el archivo de depósito masivo de detracciones (verifica el formato vigente)."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.tax import detraction_payment_file

        content = detraction_payment_file(
            ctx, invoice_ids=invoice_ids, lot=lot, company_ruc=company
        )
        output.write_text(content, encoding="ascii", newline="")
        out.line(f"Archivo {output} con {len(invoice_ids)} detracciones")
        out.data({"path": str(output), "invoices": invoice_ids})
        return ExitCode.OK

    _run(command)


@taxpayer_app.command("set")
def taxpayers_set(
    ruc: str,
    good_taxpayer: Annotated[
        bool | None, typer.Option("--good-taxpayer/--no-good-taxpayer")
    ] = None,
    retention_agent: Annotated[
        bool | None, typer.Option("--retention-agent/--no-retention-agent")
    ] = None,
    perception_agent: Annotated[
        bool | None, typer.Option("--perception-agent/--no-perception-agent")
    ] = None,
) -> None:
    """Condición tributaria de un proveedor (para calcular retenciones)."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.tax import set_supplier_status

        row = set_supplier_status(
            ctx,
            ruc,
            good_taxpayer=good_taxpayer,
            retention_agent=retention_agent,
            perception_agent=perception_agent,
        )
        out.line(
            f"{row['ruc']}: buen contribuyente {row['good_taxpayer']}, agente de retención {row['retention_agent']}, agente de percepción {row['perception_agent']}"
        )
        out.data(row)
        return ExitCode.OK

    _run(command)


@taxpayer_app.command("import-padron")
def taxpayers_import(
    path: Path, kind: Annotated[str, typer.Option(help="good | retention | perception")]
) -> None:
    """Importa un padrón de SUNAT (RUC en la primera columna)."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.tax import import_padron

        result = import_padron(ctx, path, kind=kind)
        out.line(f"{result['imported']} contribuyentes del padrón {kind}")
        out.data(result)
        return ExitCode.OK

    _run(command)


@taxpayer_app.command("retentions")
def taxpayers_retentions(
    month: Annotated[str, typer.Option(help="AAAA-MM")],
    company: Annotated[str | None, typer.Option(help="RUC de la empresa")] = None,
) -> None:
    """Retenciones del IGV (3 %) de las facturas autorizadas del mes."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.tax import retention_report

        rows = retention_report(ctx, month=month, company_ruc=company)
        for r in rows:
            if r["retention_cents"]:
                out.line(
                    f"  {r['invoice']:<16} {r['supplier_ruc']} total S/ {format_cents(r['total_pen_cents'])} → retener S/ {format_cents(r['retention_cents'])}"
                )
        out.line(f"Total a retener: S/ {format_cents(sum(r['retention_cents'] for r in rows))}")
        out.data(rows)
        return ExitCode.OK

    _run(command)


# --- Contabilidad y pagos ----------------------------------------------------------------------


@accounting_app.command("entries")
def accounting_entries(
    month: Annotated[str, typer.Option(help="AAAA-MM")],
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
    fmt: Annotated[str, typer.Option("--format", help="csv | xlsx | concar")] = "csv",
) -> None:
    """Asientos de provisión (PCGE) de las facturas autorizadas del mes."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.payables import journal, journal_csv, journal_xlsx

        lines = journal(ctx, month=month)
        if fmt not in ("csv", "xlsx", "concar"):
            raise ConfigError("--format es csv, xlsx o concar")
        if output:
            if fmt == "csv":
                output.write_text(journal_csv(lines), encoding="utf-8-sig")
            else:
                output.write_bytes(journal_xlsx(lines, concar=fmt == "concar"))
            out.line(f"{len(lines)} líneas en {output}")
        else:
            out.line(journal_csv(lines))
        out.data([line.__dict__ for line in lines])
        return ExitCode.OK

    _run(command)


@accounting_app.command("cost-center")
def accounting_cost_center(
    code: str,
    name: str,
    destination: Annotated[str | None, typer.Option(help="Cuenta de destino (clase 9)")] = None,
) -> None:
    """Crea o cambia un centro de costo (y su cuenta de destino)."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.payables import add_cost_center

        row = add_cost_center(ctx, code=code, name=name, destination_account=destination)
        out.line(f"Centro de costo {code} · {name}{' → ' + destination if destination else ''}")
        out.data(row)
        return ExitCode.OK

    _run(command)


@payments_app.command("payables")
def payments_payables(
    status: Annotated[str, typer.Option(help="scheduled | partial | paid")] = "scheduled",
) -> None:
    """Partidas por pagar."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.payables import payables

        rows = payables(ctx, status=status)
        for r in rows:
            out.line(
                f"  #{r['invoice_id']:<6} {r['invoice']:<16} {r['supplier_ruc']} {r['currency']} "
                f"{format_cents(r['pending_cents'])} el {r['scheduled_payment_date']}"
            )
        out.data(rows)
        return ExitCode.OK

    _run(command)


@payments_app.command("schedule")
def payments_schedule(invoice_id: int) -> None:
    """Programa el pago de una factura autorizada."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.payables import schedule_payment

        result = schedule_payment(ctx, invoice_id)
        out.line(json.dumps(result, ensure_ascii=False, default=_default))
        out.data(result)
        return ExitCode.OK

    _run(command)


@payments_app.command("record")
def payments_record(
    invoice_id: int,
    amount: Annotated[float, typer.Option(help="En la moneda del comprobante")],
    paid_on: Annotated[str, typer.Option("--date", help="AAAA-MM-DD")],
    reference: Annotated[str | None, typer.Option(help="Número de operación")] = None,
) -> None:
    """Registra un pago confirmado."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.payables import record_payment

        try:
            day = date.fromisoformat(paid_on)
        except ValueError as error:
            raise ConfigError("Usa la fecha AAAA-MM-DD") from error
        result = record_payment(
            ctx,
            invoice_id=invoice_id,
            amount_cents=round(amount * 100),
            paid_on=day,
            reference=reference,
            source="manual",
            created_by=ctx.reviewer,
        )
        out.line(f"Factura #{invoice_id}: {result['payment_status']}")
        out.data(result)
        return ExitCode.OK

    _run(command)


# --- Portal e indicadores ------------------------------------------------------------------------

portal_app = typer.Typer(help="Portal de proveedores: contact", no_args_is_help=True)
app.add_typer(portal_app, name="portal")


@portal_app.command("contact")
def portal_contact(supplier_ruc: str, email: str) -> None:
    """Registra un correo de contacto del proveedor (acceso al portal y avisos)."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.insights import add_contact

        row = add_contact(ctx, supplier_ruc=supplier_ruc, email=email)
        out.line(f"Contacto {row['email']} para {row['supplier_ruc']}")
        out.data(row)
        return ExitCode.OK

    _run(command)


@app.command()
def kpis(month: Annotated[str, typer.Option(help="AAAA-MM")]) -> None:
    """Indicadores del mes: procesamiento automático, tiempos de ciclo y costo de IA."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.insights import kpis as compute

        data = compute(ctx, month=month)
        for key, value in data.items():
            out.line(f"  {key:<32} {value if value is not None else '—'}")
        out.data(data)
        return ExitCode.OK

    _run(command)


# --- Trabajos en cola, worker y programador --------------------------------------------


def _wait_for_schema(ctx: AppContext, out: Out, timeout_s: float = 300) -> None:
    """Los workers no migran: esperan a que `db migrate` (o la API) deje el esquema listo."""
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            ctx.ensure_schema(migrate=False)
            return
        except Exception as error:
            if time.monotonic() >= deadline:
                raise
            typer.echo(f"Esperando a la base de datos: {error_message(error)}", err=True)
            time.sleep(10)


def _parse_payload(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError as error:
        raise ConfigError(f"--payload no es JSON válido: {error}") from error
    if not isinstance(value, dict):
        raise ConfigError('--payload debe ser un objeto JSON, p. ej. {"month": "2026-10"}')
    return value


def _job_line(job: dict[str, Any]) -> str:
    created = job.get("created_at")
    stamp = created.strftime("%Y-%m-%d %H:%M") if isinstance(created, datetime) else ""
    error = f" · {str(job['error_message'])[:80]}" if job.get("error_message") else ""
    return (
        f"#{job['id']:<7} {job['status']:<10} {job['task']:<20} "
        f"intento {job['attempts']}/{job['max_attempts']}  {stamp}{error}"
    )


@app.command()
def worker(
    queues: Annotated[
        str | None,
        typer.Option(
            help="Colas separadas por comas (por defecto todas: ingest,extract,sunat,default)"
        ),
    ] = None,
    threads: Annotated[
        int | None, typer.Option(min=1, max=64, help="Por defecto INVOICEBOT_WORKER_THREADS")
    ] = None,
) -> None:
    """Atiende la cola de Redis (Dramatiq). Escala con varias réplicas."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.jobs import QUEUES
        from invoicebot.application.tasks import registry
        from invoicebot.infrastructure.queue.redis_queue import RedisQueue, run_worker

        if ctx.config.queue.backend != "redis":
            raise ConfigError(
                "El worker necesita INVOICEBOT_QUEUE_BACKEND=redis e INVOICEBOT_REDIS_URL "
                "(sin Redis usa `invoicebot run --watch`)"
            )
        selected = [
            q.strip() for q in (queues or ",".join(registry.queues())).split(",") if q.strip()
        ]
        unknown = [q for q in selected if q not in QUEUES]
        if unknown:
            raise ConfigError(f"Colas desconocidas: {', '.join(unknown)} (usa {', '.join(QUEUES)})")
        from invoicebot.infrastructure import telemetry

        telemetry.setup("worker")
        _wait_for_schema(ctx, out)
        # Se crean ahora, en el hilo principal: los hilos del worker los comparten.
        jobs = ctx.jobs
        if "extract" in selected:
            _ = ctx.worker_deps
        queue = ctx.queue
        assert isinstance(queue, RedisQueue)
        typer.echo(
            f"InvoiceBot {__version__}: worker de {', '.join(selected)} ({queue.ping()})", err=True
        )
        run_worker(
            queue,
            queues=selected,
            threads=threads or ctx.config.queue.worker_threads,
            logger=jobs.logger,
        )
        return ExitCode.OK

    _run(command)


@app.command()
def scheduler(
    interval: Annotated[int, typer.Option(min=5, max=3600, help="Segundos entre ticks")] = 30,
) -> None:
    """Encola las tareas programadas (INVOICEBOT_SCHEDULE_*) y reenvía lo pendiente."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.scheduler import run_scheduler
        from invoicebot.infrastructure import telemetry

        telemetry.setup("scheduler")
        _wait_for_schema(ctx, out)
        typer.echo(f"InvoiceBot {__version__}: programador ({ctx.queue.ping()})", err=True)
        run_scheduler(ctx, interval_s=interval)
        return ExitCode.OK

    _run(command)


@app.command()
def enqueue(
    task: Annotated[str, typer.Argument(help="Nombre de la tarea (ver `invoicebot tasks`)")],
    payload: Annotated[str | None, typer.Option(help="Parámetros en JSON")] = None,
    wait: Annotated[bool, typer.Option("--wait", help="Espera a que termine")] = False,
    timeout: Annotated[int, typer.Option(help="Segundos máximos con --wait")] = 3600,
) -> None:
    """Encola una tarea (con la cola inline se ejecuta al momento)."""

    def command(ctx: AppContext, out: Out) -> int:
        result = ctx.jobs.enqueue(task, _parse_payload(payload), created_by=f"cli:{ctx.reviewer}")
        job = ctx.job_repository.get(result.id) or result.job
        if wait:
            deadline = time.monotonic() + timeout
            while job["status"] in ("queued", "running", "retrying"):
                if time.monotonic() >= deadline:
                    break
                time.sleep(2)
                job = ctx.job_repository.get(result.id) or job
        out.line(("Encolado: " if result.created else "Ya estaba encolado: ") + _job_line(job))
        if job.get("result") is not None and not out.json_mode:
            out.line(json.dumps(job["result"], ensure_ascii=False, indent=2, default=_default))
        out.data(job)
        return ExitCode.GENERAL_ERROR if job["status"] == "failed" else ExitCode.OK

    _run(command)


@app.command()
def tasks() -> None:
    """Tareas que pueden encolarse o programarse."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.tasks import registry

        items = [
            {"name": s.name, "queue": s.queue, "description": s.description}
            for s in sorted(registry.tasks.values(), key=lambda s: s.name)
        ]
        for item in items:
            out.line(f"{item['name']:<20} {item['queue']:<8} {item['description']}")
        out.data(items)
        return ExitCode.OK

    _run(command)


@jobs_app.command("list")
def jobs_list(
    status: Annotated[
        str | None, typer.Option(help="queued, running, retrying, succeeded, failed, cancelled")
    ] = None,
    task: Annotated[str | None, typer.Option()] = None,
    limit: Annotated[int, typer.Option(min=1, max=500)] = 30,
) -> None:
    """Últimos trabajos."""

    def command(ctx: AppContext, out: Out) -> int:
        rows = ctx.job_repository.list_jobs(status=status, task=task, limit=limit)
        for row in rows:
            out.line(_job_line(row))
        if not rows:
            out.line("Sin trabajos.")
        out.data(rows)
        return ExitCode.OK

    _run(command)


@jobs_app.command("show")
def jobs_show(job_id: int) -> None:
    """Detalle de un trabajo (parámetros, resultado y error)."""

    def command(ctx: AppContext, out: Out) -> int:
        job = ctx.job_repository.get(job_id)
        if job is None:
            raise ConfigError(f"No existe el trabajo #{job_id}")
        job["children"] = ctx.job_repository.children(job_id)
        out.line(_job_line(job))
        if not out.json_mode:
            out.line(json.dumps(job, ensure_ascii=False, indent=2, default=_default))
        out.data(job)
        return ExitCode.OK

    _run(command)


@jobs_app.command("retry")
def jobs_retry(
    job_id: int, attempts: Annotated[int, typer.Option(min=1, max=20, help="Intentos extra")] = 1
) -> None:
    """Vuelve a encolar un trabajo fallido o cancelado."""

    def command(ctx: AppContext, out: Out) -> int:
        job = ctx.job_repository.requeue(job_id, extra_attempts=attempts)
        if job is None:
            raise ConfigError(f"El trabajo #{job_id} no existe o no está fallido ni cancelado")
        ctx.jobs.publish(job)
        job = ctx.job_repository.get(job_id) or job
        out.line("Reencolado: " + _job_line(job))
        out.data(job)
        return ExitCode.OK

    _run(command)


@jobs_app.command("cancel")
def jobs_cancel(job_id: int) -> None:
    """Cancela un trabajo que todavía no empezó (o que espera un reintento)."""

    def command(ctx: AppContext, out: Out) -> int:
        if not ctx.job_repository.cancel(job_id):
            raise ConfigError(f"El trabajo #{job_id} no existe o ya empezó")
        out.line(f"Trabajo #{job_id} cancelado.")
        out.data({"id": job_id, "status": "cancelled"})
        return ExitCode.OK

    _run(command)


@jobs_app.command("run")
def jobs_run(job_id: int) -> None:
    """Ejecuta un trabajo en este proceso (diagnóstico)."""

    def command(ctx: AppContext, out: Out) -> int:
        job = ctx.jobs.run_now(job_id)
        if job is None:
            raise ConfigError(f"No existe el trabajo #{job_id}")
        out.line(_job_line(job))
        out.data(job)
        return ExitCode.GENERAL_ERROR if job["status"] == "failed" else ExitCode.OK

    _run(command)


@schedule_app.command("list")
def schedule_list() -> None:
    """Programaciones con su próxima ejecución."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.scheduler import configured_schedules

        ctx.job_repository.sync_schedules(configured_schedules(ctx.config))
        rows = ctx.job_repository.list_schedules()
        for row in rows:
            state = "activa" if row["enabled"] else "inactiva"
            nxt = row["next_run_at"]
            when = nxt.astimezone().strftime("%Y-%m-%d %H:%M") if isinstance(nxt, datetime) else "—"
            out.line(
                f"{row['name']:<10} {row['cron']:<18} {row['task']:<18} {state:<9} próxima {when} ({row['source']})"
            )
        out.data(rows)
        return ExitCode.OK

    _run(command)


@schedule_app.command("tick")
def schedule_tick() -> None:
    """Un solo tick del programador (Container Apps Job o tarea programada)."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.scheduler import tick

        _wait_for_schema(ctx, out, timeout_s=120)
        result = tick(ctx)
        if result.skipped:
            out.line(f"Omitido: {result.skipped}")
        for item in result.enqueued:
            out.line(
                f"Encolado {item['schedule']} → trabajo #{item['job_id']}"
                + ("" if item["new"] else " (ya activo)")
            )
        if result.relayed:
            out.line(f"Reenviados: {result.relayed}")
        for error in result.errors:
            typer.echo(f"Error: {error}", err=True)
        out.data(result.as_dict())
        return ExitCode.PARTIAL if result.errors else ExitCode.OK

    _run(command)


@schedule_app.command("set")
def schedule_set(
    name: str,
    cron: Annotated[str, typer.Argument(help='Cron de 5 campos, p. ej. "0 7 * * 1-5"')],
    task: str,
    payload: Annotated[str | None, typer.Option(help="Parámetros en JSON")] = None,
) -> None:
    """Crea o cambia una programación manual (no la sobrescribe la configuración)."""

    def command(ctx: AppContext, out: Out) -> int:
        from invoicebot.application.tasks import registry
        from invoicebot.domain.cron import CronError, parse_cron

        registry.get(task)
        try:
            parse_cron(cron)
        except CronError as error:
            raise ConfigError(f"Cron no válido: {error}") from error
        ctx.job_repository.upsert_manual_schedule(
            name=name, task=task, cron=cron, payload=_parse_payload(payload)
        )
        out.line(f"Programación {name}: {cron} → {task}")
        out.data({"name": name, "cron": cron, "task": task})
        return ExitCode.OK

    _run(command)


def _set_schedule_enabled(name: str, enabled: bool) -> None:
    def command(ctx: AppContext, out: Out) -> int:
        if not ctx.job_repository.set_schedule_enabled(name, enabled):
            raise ConfigError(f"No existe la programación {name!r}")
        out.line(
            f"Programación {name} {'activada' if enabled else 'desactivada'} (ahora es manual)"
        )
        out.data({"name": name, "enabled": enabled})
        return ExitCode.OK

    _run(command)


@schedule_app.command("enable")
def schedule_enable(name: str) -> None:
    """Activa una programación."""
    _set_schedule_enabled(name, True)


@schedule_app.command("disable")
def schedule_disable(name: str) -> None:
    """Desactiva una programación."""
    _set_schedule_enabled(name, False)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
