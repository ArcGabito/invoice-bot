"""Tareas de InvoiceBot que pueden encolarse (`invoicebot enqueue`, la API o el programador).

| Tarea | Cola | Qué hace |
| --- | --- | --- |
| ``inbox.collect`` | ingest | Lee el correo y la carpeta de entrada; registra y encola lo nuevo |
| ``documents.scan`` | ingest | Registra los documentos de una carpeta y los encola |
| ``documents.process`` | extract | Procesa un documento: XML/PDF/OCR/IA, validación y guardado |
| ``documents.retry`` | ingest | Reencola los documentos fallidos por errores temporales |
| ``fx.fetch`` | sunat | Descarga el tipo de cambio del día |
| ``sunat.verify`` | sunat | Verifica en SUNAT los comprobantes pendientes |
| ``exports.monthly`` | default | Exporta el mes actual y el anterior |
| ``db.backup`` | default | Copia de seguridad (si toca) |
| ``jobs.purge`` | default | Borra los trabajos terminados antiguos |
| ``bot.cycle`` | default | El ciclo completo de `invoicebot run` (modo clásico) |
| ``purchasing.match`` | default | Match con la orden de compra, nota aplicada y cuenta contable |
| ``approvals.start`` | default | Empieza la aprobación de pago de una factura revisada |
| ``approvals.remind`` | default | Recordatorios y escalamiento de aprobaciones vencidas |
| ``webhooks.deliver`` | default | Entrega firmada de un evento a un webhook |
| ``webhooks.sweep`` | default | Encola las entregas pendientes que quedaron sin trabajo |
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from invoicebot.application.jobs import PermanentJobError, TaskRegistry
from invoicebot.errors import LockedError, error_message

if TYPE_CHECKING:
    from invoicebot.context import AppContext

registry = TaskRegistry()


def _months(payload: dict[str, Any], today: str) -> list[str]:
    from invoicebot.application.reports import check_month
    from invoicebot.application.run_bot import current_and_previous_month

    months = payload.get("months")
    if not months:
        return list(current_and_previous_month(today))
    if not isinstance(months, list):
        raise PermanentJobError("months debe ser una lista de meses AAAA-MM")
    return [str(check_month(str(m))) for m in months]


@registry.task(
    "inbox.collect",
    queue="ingest",
    description="Correo y carpeta de entrada: registra los documentos nuevos y los encola",
)
def collect(ctx: AppContext, payload: dict[str, Any]) -> dict[str, Any]:
    from invoicebot.application.fetch_mail import fetch_mail
    from invoicebot.application.processing import scan_invoices
    from invoicebot.application.run_bot import _is_inside

    config, deps = ctx.config, ctx.worker_deps
    result: dict[str, Any] = {}
    roots: list[tuple[str, bool]] = []
    if config.run.inbox_dir:
        roots.append((config.run.inbox_dir, config.run.recursive))
    if config.mail.provider != "none":
        mail_dir = ctx.mail_dir()
        try:
            result["mail"] = fetch_mail(
                deps, source=ctx.mail_source(), directory=mail_dir
            ).as_dict()
        except Exception as error:
            # Un buzón caído no impide procesar lo que ya está en la carpeta.
            result["mail_error"] = error_message(error)
            ctx.logger.error("no se pudo leer el correo: %s", result["mail_error"])
        if not (
            config.run.inbox_dir
            and config.run.recursive
            and _is_inside(mail_dir, config.run.inbox_dir)
        ):
            roots.append((mail_dir, True))
    if not roots:
        raise PermanentJobError("Define INVOICEBOT_INBOX_DIR o un buzón (INVOICEBOT_MAIL_PROVIDER)")
    scans = []
    for root, recursive in roots:
        Path(root).mkdir(parents=True, exist_ok=True)
        scans.append(
            scan_invoices(deps, root=root, recursive=recursive, dispatch=ctx.dispatch_document)
        )
    total = scans[0]
    for other in scans[1:]:
        total = total.merge(other)
    result["scan"] = total.as_dict()
    if "mail_error" in result:
        # El trabajo termina (lo escaneado ya está encolado), pero queda constancia del fallo.
        result["partial"] = True
    return result


@registry.task(
    "documents.scan",
    queue="ingest",
    description="Registra los documentos de una carpeta (o un archivo) y los encola",
)
def scan(ctx: AppContext, payload: dict[str, Any]) -> dict[str, Any]:
    from invoicebot.application.processing import scan_invoices

    root = payload.get("root")
    if not root:
        raise PermanentJobError("Falta root (carpeta o archivo)")
    summary = scan_invoices(
        ctx.worker_deps,
        root=str(root),
        recursive=bool(payload.get("recursive", False)),
        dispatch=ctx.dispatch_document,
    )
    return summary.as_dict()


@registry.task(
    "documents.process",
    queue="extract",
    description="Procesa un documento registrado (XML, PDF, OCR o IA) y lo valida",
    time_limit_s=15 * 60,
)
def process(ctx: AppContext, payload: dict[str, Any]) -> dict[str, Any]:
    from dataclasses import asdict

    from invoicebot.application.processing import process_document_by_id

    document_id = payload.get("document_id")
    if not isinstance(document_id, int):
        raise PermanentJobError("Falta document_id (número)")
    return asdict(process_document_by_id(ctx.worker_deps, document_id))


@registry.task(
    "documents.retry",
    queue="ingest",
    description="Reencola los documentos fallidos por errores temporales y los pendientes",
)
def retry(ctx: AppContext, payload: dict[str, Any]) -> dict[str, Any]:
    from invoicebot.application.processing import retry_documents

    summary = retry_documents(
        ctx.worker_deps,
        failed=bool(payload.get("failed", True)),
        pending=bool(payload.get("pending", True)),
        force=bool(payload.get("force", False)),
        dispatch=ctx.dispatch_document,
    )
    return summary.as_dict()


@registry.task("fx.fetch", queue="sunat", description="Tipo de cambio del día (SUNAT)")
def fetch_fx(ctx: AppContext, _payload: dict[str, Any]) -> dict[str, Any]:
    from invoicebot.application.exchange_rates import save_exchange_rates
    from invoicebot.infrastructure.sunat import SUNAT_EXCHANGE_RATE_URL, SunatExchangeRateSource

    config = ctx.config
    if not config.fx.sunat:
        return {"skipped": "INVOICEBOT_FX_SUNAT=false"}
    source = SunatExchangeRateSource(
        url=config.fx.sunat_url or SUNAT_EXCHANGE_RATE_URL, timeout_s=config.fx.timeout_s
    )
    with ctx.rate_limiter("sunat").slot():
        today = source.fetch_today()
    applied = save_exchange_rates(ctx.review_deps(), [today])
    return {"date": today["date"], "sell": today["sell"], **applied}


@registry.task(
    "sunat.verify",
    queue="sunat",
    description="Verifica en SUNAT los comprobantes pendientes",
    time_limit_s=60 * 60,
)
def verify(ctx: AppContext, payload: dict[str, Any]) -> dict[str, Any]:
    from invoicebot.application.verify_sunat import verify_sunat

    if not ctx.config.sunat.enabled:
        return {"skipped": "INVOICEBOT_SUNAT_ENABLED=false"}
    summary = verify_sunat(
        ctx.review_deps(),
        client=ctx.limited_sunat_client(),
        month=payload.get("month"),
        force=bool(payload.get("force", False)),
        limit=payload.get("limit"),
    )
    return summary.as_dict()


@registry.task("exports.monthly", queue="default", description="Exporta a CSV o Excel")
def export(ctx: AppContext, payload: dict[str, Any]) -> dict[str, Any]:
    from invoicebot.application.reports import export_invoices

    fmt = str(payload.get("format") or ctx.config.run.export)
    if fmt == "none":
        fmt = ctx.config.export_format
    if fmt not in ("csv", "xlsx"):
        raise PermanentJobError("format debe ser csv o xlsx")
    deps = ctx.review_deps()
    files = []
    for month in _months(payload, deps.today):
        result = export_invoices(deps, month=month, fmt=fmt)
        files.append({"month": month, "path": result.path, "rows": result.rows})
    return {"files": files}


@registry.task("db.backup", queue="default", description="Copia de seguridad de SQL Server")
def backup(ctx: AppContext, payload: dict[str, Any]) -> dict[str, Any]:
    from invoicebot.infrastructure.db.backup import create_backup, is_backup_due

    db, config = ctx.db, ctx.config
    if db.is_azure_sql():
        return {"skipped": "Azure SQL tiene copias automáticas"}
    if not payload.get("force") and not is_backup_due(db, config.backup.interval_hours):
        return {"skipped": "la última copia es reciente"}
    result = create_backup(db, config.backup.dir, retention=config.backup.retention)
    return {"path": result.path, "size": result.size, "removed": result.removed}


@registry.task("jobs.purge", queue="default", description="Borra los trabajos terminados antiguos")
def purge(ctx: AppContext, payload: dict[str, Any]) -> dict[str, Any]:
    days = int(payload.get("days") or ctx.config.schedule.job_retention_days)
    return {"deleted": ctx.job_repository.purge(older_than_days=days)}


@registry.task(
    "bot.cycle",
    queue="default",
    description="Ciclo completo de `invoicebot run` en un solo trabajo",
    max_attempts=1,
    time_limit_s=2 * 60 * 60,
)
def cycle(ctx: AppContext, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        result = ctx.run_cycle(trigger=str(payload.get("trigger") or "queue"))
    except LockedError as error:
        return {"skipped": error.message}
    return {
        "run_id": result.run_id,
        "status": result.status,
        "exit_code": result.exit_code,
        "error": result.error,
        "summary": result.report.compact(),
    }


@registry.task(
    "purchasing.match",
    queue="default",
    description="Match de una factura con su orden de compra y recepción",
)
def purchasing_match(ctx: AppContext, payload: dict[str, Any]) -> dict[str, Any]:
    from invoicebot.application.purchasing import run_match

    invoice_id = payload.get("invoice_id")
    if not isinstance(invoice_id, int):
        raise PermanentJobError("Falta invoice_id (número)")
    return run_match(ctx, invoice_id, po_number=payload.get("po_number"))


@registry.task(
    "approvals.start", queue="default", description="Empieza la aprobación de pago de una factura"
)
def approvals_start(ctx: AppContext, payload: dict[str, Any]) -> dict[str, Any]:
    from invoicebot.application.approvals import start

    invoice_id = payload.get("invoice_id")
    if not isinstance(invoice_id, int):
        raise PermanentJobError("Falta invoice_id (número)")
    return start(ctx, invoice_id, restart=bool(payload.get("restart")))


@registry.task(
    "approvals.remind", queue="default", description="Recordatorios y escalamiento de aprobaciones"
)
def approvals_remind(ctx: AppContext, _payload: dict[str, Any]) -> dict[str, Any]:
    from invoicebot.application.approvals import remind

    return remind(ctx)


@registry.task(
    "webhooks.deliver",
    queue="default",
    description="Entrega firmada de un evento a un webhook",
    # Los reintentos los cuenta la entrega (MAX_DELIVERY_ATTEMPTS); el trabajo admite más.
    max_attempts=12,
    time_limit_s=60,
)
def webhooks_deliver(ctx: AppContext, payload: dict[str, Any]) -> dict[str, Any]:
    from invoicebot.application.events import deliver

    delivery_id = payload.get("delivery_id")
    if not isinstance(delivery_id, int):
        raise PermanentJobError("Falta delivery_id (número)")
    return deliver(ctx, delivery_id)


@registry.task("webhooks.sweep", queue="default", description="Encola las entregas pendientes")
def webhooks_sweep(ctx: AppContext, _payload: dict[str, Any]) -> dict[str, Any]:
    from invoicebot.application.events import dispatch

    return {"queued": dispatch(ctx)}
