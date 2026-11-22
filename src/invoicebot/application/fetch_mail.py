"""Descarga de comprobantes adjuntos desde el buzón a la carpeta de entrada."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from invoicebot.application.deps import Deps
from invoicebot.application.ports import MailSource
from invoicebot.errors import error_message
from invoicebot.infrastructure.mail.attachments import extract_mail_files
from invoicebot.infrastructure.mail.base import MailError


@dataclass
class MailSummary:
    account: str = ""
    listed: int = 0
    read: int = 0
    saved: int = 0
    files: int = 0
    no_attachments: int = 0
    failed: int = 0
    #: Mensajes nuevos que quedan para el siguiente ciclo (INVOICEBOT_MAIL_MAX_MESSAGES).
    remaining: int = 0
    moved: int = 0
    move_failed: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def message_folder(moment: datetime, message_id: str) -> str:
    """Una carpeta por mensaje: 2026-10/20261015-140309-1a2b3c4d."""
    utc = moment.astimezone(UTC) if moment.tzinfo else moment.replace(tzinfo=UTC)
    digest = hashlib.sha256(message_id.encode()).hexdigest()[:8]
    return f"{utc:%Y-%m}/{utc:%Y%m%d-%H%M%S}-{digest}"


def _naive_utc(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment.astimezone(UTC).replace(tzinfo=None) if moment.tzinfo else moment


def fetch_mail(
    deps: Deps, *, source: MailSource, directory: str, now: datetime | None = None
) -> MailSummary:
    """Guarda los adjuntos de los correos nuevos en `directory`, juntos por mensaje.

    Así el PDF se vincula con su XML. Cada mensaje se lee una sola vez; los que
    fallan se reintentan hasta INVOICEBOT_MAX_ATTEMPTS. Con carpetas de
    procesados/revisión, cada mensaje leído se mueve (si falla, se reintenta en
    el ciclo siguiente). Si el servidor rechaza las credenciales se detiene.
    """
    repo, log, config = deps.repository, deps.logger, deps.config
    now = now or datetime.now(UTC)
    address = source.connect()
    account = f"{source.provider}:{address.lower()}"
    try:
        refs = source.list_messages(since=now - timedelta(days=config.mail.since_days))
        known = repo.find_mail_messages(account, [r.id for r in refs])
        pending = [
            r
            for r in refs
            if r.id not in known
            or (known[r.id]["status"] == "failed" and known[r.id]["attempts"] < config.max_attempts)
        ]
        batch = pending[: config.mail.max_messages]
        summary = MailSummary(
            account=account, listed=len(refs), remaining=len(pending) - len(batch)
        )
        targets = {
            "saved": config.mail.processed_folder,
            "no_attachments": config.mail.review_folder,
        }

        def move(message_id: str, record_id: int, status: str) -> None:
            target = targets.get(status)
            if not target:
                return
            try:
                source.move(message_id, target)
                repo.set_mail_message_moved(record_id, target)
                summary.moved += 1
            except Exception as error:
                summary.move_failed += 1
                log.warning(
                    "no se pudo mover el mensaje %s a %s: %s",
                    message_id,
                    target,
                    error_message(error),
                )

        # Leídos antes que aún no se movieron (p. ej. se activó después o faltaba permiso).
        for ref in refs:
            seen = known.get(ref.id)
            if seen and not seen["moved_to"] and seen["status"] != "failed":
                move(ref.id, seen["id"], seen["status"])

        for ref in batch:
            record: dict[str, Any] = {
                "account": account,
                "message_id": ref.id,
                "received_at": _naive_utc(ref.received_at),
                "sender": None,
                "subject": None,
            }
            try:
                mail = extract_mail_files(source.fetch_raw(ref.id))
                record["sender"] = (mail.sender or "")[:320] or None
                record["subject"] = (mail.subject or "")[:500] or None
                if record["received_at"] is None and isinstance(mail.date, datetime):
                    record["received_at"] = _naive_utc(mail.date)
                if mail.skipped:
                    log.info("adjuntos omitidos en %s: %s", ref.id, "; ".join(mail.skipped))
                summary.read += 1
                if not mail.files:
                    summary.no_attachments += 1
                    record_id = repo.record_mail_message(
                        **record, status="no_attachments", files=0, folder=None, error_message=None
                    )
                    move(ref.id, record_id, "no_attachments")
                    continue
                moment = ref.received_at or (mail.date if isinstance(mail.date, datetime) else now)
                folder = message_folder(moment, ref.id)
                target = Path(directory) / folder
                target.mkdir(parents=True, exist_ok=True)
                for f in mail.files:
                    (target / f.name).write_bytes(f.content)
                record_id = repo.record_mail_message(
                    **record,
                    status="saved",
                    files=len(mail.files),
                    folder=folder,
                    error_message=None,
                    attachments=[
                        (hashlib.sha256(f.content).hexdigest(), f.name) for f in mail.files
                    ],
                )
                move(ref.id, record_id, "saved")
                summary.saved += 1
                summary.files += len(mail.files)
                log.info(
                    "comprobantes descargados del correo: %s", ", ".join(f.name for f in mail.files)
                )
            except MailError as error:
                if error.auth:
                    raise
                summary.failed += 1
                repo.record_mail_message(
                    **record, status="failed", files=0, folder=None, error_message=error.message
                )
            except Exception as error:
                summary.failed += 1
                log.warning("no se pudo leer el mensaje %s: %s", ref.id, error_message(error))
                repo.record_mail_message(
                    **record,
                    status="failed",
                    files=0,
                    folder=None,
                    error_message=error_message(error),
                )
        return summary
    finally:
        source.close()
