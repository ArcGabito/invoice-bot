"""Repositorio de documentos, extracciones, facturas e incidencias (SQLAlchemy 2.0 ORM).

Las consultas se construyen con los modelos de ``models.py`` (``select``, ``insert``,
``update``, ``delete``) y se ejecutan sobre la conexión de la transacción en curso.
Solo queda en SQL de texto lo que es propio de SQL Server: ``sp_getapplock``.

Garantías (las que esperan los casos de uso):
- `save_invoice_result` es atómico (factura + incidencias + estado del documento).
- `insert_document` rechaza un SHA-256 ya registrado (restricción única).
- `transaction()` devuelve un repositorio ligado a una transacción.
- `lock_business_key` serializa, hasta el final de la transacción, a quienes buscan
  duplicados de una misma clave de negocio y guardan la factura.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Connection,
    Executable,
    Unicode,
    and_,
    case,
    cast,
    delete,
    exists,
    func,
    insert,
    literal,
    literal_column,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from invoicebot.domain.exchange_rate import EXCHANGE_RATE_LOOKBACK_DAYS, ExchangeRate
from invoicebot.domain.invoice import (
    INVOICE_COLUMNS,
    DataSource,
    DocumentStatus,
    InvoiceData,
    NewIssue,
)
from invoicebot.domain.sunat_status import SUNAT_RULE_PREFIX, SunatResult
from invoicebot.domain.validation import is_calendar_date, status_from_issues
from invoicebot.infrastructure.db.connection import Database
from invoicebot.infrastructure.db.models import (
    BotRun,
    Company,
    Document,
    ExtractionRun,
    FileOccurrence,
    Invoice,
    InvoiceChange,
    MailAttachment,
    MailMessage,
    ReviewAction,
    SunatCheck,
    ValidationIssue,
)
from invoicebot.infrastructure.db.models import ExchangeRate as ExchangeRateRow

Row = dict[str, Any]
_NOW = func.sysutcdatetime()
_INVOICES = Invoice.__table__.c

_DOCUMENT_FIELDS = frozenset(
    {
        "status",
        "text_method",
        "text_chars",
        "page_count",
        "error_kind",
        "error_message",
        "skip_reason",
        "linked_document_id",
        "file_path",
        "file_name",
    }
)

#: Columnas de los listados (factura + archivo).
_LIST_COLUMNS = (
    Invoice.id.label("invoice_id"),
    Invoice.document_id,
    Invoice.validation_status,
    Invoice.review_status,
    Invoice.supplier_name,
    Invoice.supplier_ruc,
    Invoice.document_type,
    Invoice.series,
    Invoice.number,
    Invoice.issue_date,
    Invoice.currency,
    Invoice.total_cents,
    Invoice.data_source,
    Invoice.sunat_status,
    Invoice.reviewed_by,
    Invoice.reviewed_at,
    Document.file_name,
    Document.file_path,
)


def as_row(mapping: Any) -> Row:
    """Fila como dict: DATETIME2 (UTC, sin zona) → datetime con zona UTC; DECIMAL → float.

    Los importes van en céntimos (BIGINT): los DECIMAL son tasas y tipos de cambio.
    """
    row: Row = {}
    for key, value in mapping.items():
        if isinstance(value, datetime) and value.tzinfo is None:
            row[key] = value.replace(tzinfo=UTC)
        elif isinstance(value, Decimal):
            row[key] = float(value)
        else:
            row[key] = value
    return row


def _rows(result: Any) -> list[Row]:
    return [as_row(r) for r in result.mappings()]


def _first(result: Any) -> Row | None:
    row = result.mappings().first()
    return as_row(row) if row is not None else None


def month_range(month: str) -> tuple[date, date]:
    """ "2026-10" → (2026-10-01, 2026-11-01): rango que aprovecha el índice de issue_date."""
    year, mon = (int(p) for p in month.split("-"))
    start = date(year, mon, 1)
    end = date(year + 1, 1, 1) if mon == 12 else date(year, mon + 1, 1)
    return start, end


def storable_date(value: str | None) -> date | None:
    """Una fecha que no existe (31 de febrero) no cabe en DATE: queda NULL (la validación ya la registró)."""
    if not value or not is_calendar_date(value):
        return None
    return date.fromisoformat(value)


def _invoice_values(invoice: InvoiceData) -> Row:
    values = {column: invoice.get(column) for column in INVOICE_COLUMNS}
    values["issue_date"] = storable_date(invoice.issue_date)
    values["due_date"] = storable_date(invoice.due_date)
    return values


def _clip(value: object, size: int) -> str | None:
    return None if value is None else str(value)[:size]


def _path_hash(file_path: str) -> Any:
    """HASHBYTES igual que la columna calculada file_occurrences.path_hash (índice)."""
    # El algoritmo debe ir literal: como parámetro, HASHBYTES devuelve NULL.
    return func.HASHBYTES(literal_column("'SHA2_256'"), cast(literal(file_path), Unicode(1024)))


def _open_issues(invoice_id: int) -> tuple[Any, Any]:
    return ValidationIssue.invoice_id == invoice_id, ValidationIssue.resolved_at.is_(None)


class InvoiceRepository:
    """Implementación SQL Server. Dentro de `transaction()` todo usa la misma transacción."""

    def __init__(self, db: Database, conn: Connection | None = None):
        self.db = db
        self.conn = conn

    @property
    def in_transaction(self) -> bool:
        return self.conn is not None

    @contextmanager
    def _c(self) -> Iterator[Connection]:
        if self.conn is not None:
            yield self.conn
        else:
            with self.db.begin() as conn:
                yield conn

    @contextmanager
    def transaction(self) -> Iterator[InvoiceRepository]:
        """O se aplican todos los cambios o ninguno."""
        if self.conn is not None:
            yield self
            return
        with self.db.begin() as conn:
            yield InvoiceRepository(self.db, conn)

    def _all(self, statement: Executable) -> list[Row]:
        with self._c() as conn:
            return _rows(conn.execute(statement))

    def _one(self, statement: Executable) -> Row | None:
        with self._c() as conn:
            return _first(conn.execute(statement))

    def _exec(self, statement: Executable, params: list[dict[str, Any]] | None = None) -> int:
        with self._c() as conn:
            result = conn.execute(statement, params) if params else conn.execute(statement)
            return int(getattr(result, "rowcount", 0) or 0)

    # --- Documentos ---------------------------------------------------------

    def find_document_by_sha(self, sha256: str) -> Row | None:
        return self._one(select(Document).where(Document.sha256 == sha256))

    def get_document(self, document_id: int) -> Row | None:
        return self._one(select(Document).where(Document.id == document_id))

    def find_document_by_path(self, file_path: str) -> Row | None:
        """Documento visto por última vez en `file_path` (según file_occurrences)."""
        return self._one(
            select(Document)
            .join(FileOccurrence, FileOccurrence.document_id == Document.id)
            .where(
                FileOccurrence.path_hash == _path_hash(file_path),
                FileOccurrence.file_path == file_path,
            )
        )

    def insert_document(
        self, *, sha256: str, file_path: str, file_name: str, mime_type: str, file_size: int
    ) -> Row:
        row = self._one(
            insert(Document)
            .values(
                sha256=sha256,
                file_path=file_path,
                file_name=file_name[:260],
                mime_type=mime_type,
                file_size=file_size,
                status="pending",
            )
            .returning(Document)
        )
        assert row is not None
        return row

    def record_occurrence(self, document_id: int, file_path: str) -> bool:
        """Registra que el documento se vio en `file_path`. True si la ruta es nueva para él."""
        with self._c() as conn:
            existing = _first(
                conn.execute(
                    select(FileOccurrence.id, FileOccurrence.document_id).where(
                        FileOccurrence.path_hash == _path_hash(file_path),
                        FileOccurrence.file_path == file_path,
                    )
                )
            )
            if existing:
                # El archivo de esa ruta pudo cambiar de contenido: se reasigna.
                conn.execute(
                    update(FileOccurrence)
                    .where(FileOccurrence.id == existing["id"])
                    .values(document_id=document_id, last_seen_at=_NOW)
                )
                return bool(existing["document_id"] != document_id)
            conn.execute(
                insert(FileOccurrence).values(document_id=document_id, file_path=file_path)
            )
            return True

    def get_occurrences(self, document_id: int) -> list[Row]:
        return self._all(
            select(
                FileOccurrence.file_path, FileOccurrence.first_seen_at, FileOccurrence.last_seen_at
            )
            .where(FileOccurrence.document_id == document_id)
            .order_by(FileOccurrence.id)
        )

    def start_attempt(self, document_id: int) -> int:
        """Marca el documento en proceso e incrementa su contador de intentos."""
        row = self._one(
            update(Document)
            .where(Document.id == document_id)
            .values(
                status="processing",
                attempts=Document.attempts + 1,
                error_kind=None,
                error_message=None,
                updated_at=_NOW,
            )
            .returning(Document.attempts)
        )
        assert row is not None
        return int(row["attempts"])

    def update_document(self, document_id: int, *, processed: bool = False, **fields: Any) -> None:
        unknown = set(fields) - _DOCUMENT_FIELDS
        if unknown:
            raise ValueError(f"Columnas de documento desconocidas: {sorted(unknown)}")
        if "error_message" in fields:
            fields["error_message"] = _clip(fields["error_message"], 4000)
        if processed:
            fields["processed_at"] = _NOW
        if not fields:
            return
        self._exec(
            update(Document).where(Document.id == document_id).values(updated_at=_NOW, **fields)
        )

    def reset_interrupted(self) -> int:
        """Documentos que quedaron "en proceso" por una ejecución interrumpida."""
        return self._exec(
            update(Document)
            .where(Document.status == "processing")
            .values(status="pending", updated_at=_NOW)
        )

    def list_documents_by_status(self, statuses: Sequence[str]) -> list[Row]:
        if not statuses:
            return []
        return self._all(
            select(Document).where(Document.status.in_(list(statuses))).order_by(Document.id)
        )

    # --- Extracciones --------------------------------------------------------

    def insert_extraction_run(self, **run: Any) -> int:
        row = self._one(
            insert(ExtractionRun)
            .values(
                document_id=run["document_id"],
                attempt=run["attempt"],
                provider=run["provider"],
                model=run.get("model"),
                prompt_version=run["prompt_version"],
                text_method=run.get("text_method"),
                input_chars=run.get("input_chars"),
                status=run["status"],
                error_kind=run.get("error_kind"),
                error_message=_clip(run.get("error_message"), 4000),
                raw_response=run.get("raw_response"),
                input_tokens=run.get("input_tokens"),
                output_tokens=run.get("output_tokens"),
                duration_ms=run.get("duration_ms"),
            )
            .returning(ExtractionRun.id)
        )
        assert row is not None
        return int(row["id"])

    def list_extraction_runs(self, document_id: int) -> list[Row]:
        return self._all(
            select(ExtractionRun)
            .where(ExtractionRun.document_id == document_id)
            .order_by(ExtractionRun.id)
        )

    # --- Facturas ------------------------------------------------------------

    def save_invoice_result(
        self,
        *,
        document_id: int,
        extraction_run_id: int,
        invoice: InvoiceData,
        validation_status: str,
        rules_version: str,
        business_key: str | None,
        data_source: str = DataSource.AI,
        issues: Sequence[NewIssue],
        exchange_rate: ExchangeRate | None = None,
        company_id: int | None = None,
    ) -> int:
        """Guarda factura, incidencias y estado del documento. Reemplaza la factura anterior."""
        values = {
            **_invoice_values(invoice),
            "company_id": company_id,
            "exchange_rate": exchange_rate.rate if exchange_rate else None,
            "exchange_rate_date": storable_date(exchange_rate.date) if exchange_rate else None,
            "document_id": document_id,
            "extraction_run_id": extraction_run_id,
            "data_source": data_source,
            "business_key": business_key,
            "validation_status": validation_status,
            "rules_version": rules_version,
        }
        with self.transaction() as repo, repo._c() as conn:
            existing = conn.execute(
                select(Invoice.id).where(Invoice.document_id == document_id)
            ).scalar()
            if existing:
                conn.execute(
                    update(Invoice)
                    .where(Invoice.id == existing)
                    .values(
                        **values,
                        updated_at=_NOW,
                        sunat_status=None,
                        sunat_checked_at=None,
                        review_status="pending",
                        reviewed_at=None,
                        reviewed_by=None,
                        corrected=False,
                    )
                )
                conn.execute(delete(ValidationIssue).where(ValidationIssue.invoice_id == existing))
                invoice_id = int(existing)
            else:
                # `invoices` tiene un trigger de auditoría: sin OUTPUT, el id llega por
                # SCOPE_IDENTITY (implicit_returning=False en el modelo).
                key = conn.execute(insert(Invoice).values(**values)).inserted_primary_key
                assert key is not None
                invoice_id = int(key[0])
            repo.insert_issues(invoice_id, issues)
            repo.update_document(
                document_id,
                processed=True,
                status=DocumentStatus.EXTRACTED,
                error_kind=None,
                error_message=None,
            )
        return invoice_id

    def company_for_customer(self, customer_ruc: str | None) -> tuple[int | None, bool]:
        """(empresa activa con ese RUC, ¿hay empresas registradas?)."""
        active = Company.active == True  # noqa: E712 (SQL Server: = 1)
        company = (
            select(Company.id)
            .where(Company.ruc == (customer_ruc or ""), active)
            .limit(1)
            .scalar_subquery()
        )
        count = select(func.count()).select_from(Company).where(active).scalar_subquery()
        row = self._one(select(company.label("id"), count.label("n")))
        if row is None:
            return None, False
        return (int(row["id"]) if row["id"] is not None else None), int(row["n"] or 0) > 0

    def invoice_history(self, invoice_id: int) -> list[Row]:
        """Cada estado de la factura (trigger de auditoría): qué cambió, cuándo y quién.

        Respeta la Row-Level Security: solo se muestra si la factura es visible.
        """
        c = InvoiceChange
        return self._all(
            select(
                c.id,
                c.operation,
                c.changed_at,
                c.changed_by,
                c.validation_status,
                c.review_status,
                c.reviewed_by,
                c.supplier_ruc,
                c.document_type,
                c.series,
                c.number,
                c.issue_date,
                c.currency,
                c.total_cents,
                c.igv_cents,
                c.sunat_status,
                c.company_id,
                c.corrected,
                c.approval_status,
                c.account_code,
                c.cost_center,
            )
            .where(c.invoice_id == invoice_id, exists().where(Invoice.id == invoice_id))
            .order_by(c.id)
        )

    def insert_issues(self, invoice_id: int, issues: Sequence[NewIssue]) -> None:
        if not issues:
            return
        self._exec(
            insert(ValidationIssue),
            [
                {
                    "invoice_id": invoice_id,
                    "rule_code": issue.rule_code,
                    "severity": issue.severity,
                    "message": issue.message[:1000],
                    "expected_value": _clip(issue.expected_value, 1000),
                    "actual_value": _clip(issue.actual_value, 1000),
                }
                for issue in issues
            ],
        )

    def get_invoice_by_document(self, document_id: int) -> Row | None:
        return self._one(select(*_INVOICES).where(Invoice.document_id == document_id))

    def get_issues(self, invoice_id: int) -> list[Row]:
        return self._all(
            select(ValidationIssue)
            .where(ValidationIssue.invoice_id == invoice_id)
            .order_by(ValidationIssue.id)
        )

    def lock_business_key(self, key: str) -> None:
        """Reserva la clave de negocio hasta el final de la transacción en curso.

        `sp_getapplock` no tiene equivalente en el ORM: es lo único en SQL de texto.
        """
        if not self.in_transaction:
            raise RuntimeError("lock_business_key requiere una transacción")
        resource = f"invoicebot-bk|{key}"[:255]
        with self._c() as conn:
            result = conn.execute(
                text(
                    """SET NOCOUNT ON; DECLARE @r INT;
                       EXEC @r = sp_getapplock @Resource = :res, @LockMode = 'Exclusive',
                         @LockOwner = 'Transaction', @LockTimeout = 60000;
                       SELECT @r AS result"""
                ),
                {"res": resource},
            ).scalar()
        if result is None or int(result) < 0:
            raise RuntimeError(f"No se pudo reservar la clave de negocio {key}")

    def find_by_business_key(self, key: str, exclude_document_id: int) -> list[Row]:
        """Otras facturas con la misma clave de negocio, de documentos distintos."""
        return self._all(
            select(Invoice.id, Invoice.document_id, Document.file_name, Document.file_path)
            .join(Document, Document.id == Invoice.document_id)
            .where(
                Invoice.business_key == key,
                Invoice.document_id != exclude_document_id,
                Invoice.review_status != "rejected",
            )
            .order_by(Invoice.id)
        )

    # --- Tipo de cambio -----------------------------------------------------

    def find_exchange_rate(self, currency: str, on: str) -> ExchangeRate | None:
        """Tipo de cambio venta de `currency` para `on`, o el último publicado antes."""
        day = date.fromisoformat(on)
        row = self._one(
            select(ExchangeRateRow.sell, ExchangeRateRow.rate_date)
            .where(
                ExchangeRateRow.currency == currency,
                ExchangeRateRow.rate_date <= day,
                ExchangeRateRow.rate_date >= day - timedelta(days=EXCHANGE_RATE_LOOKBACK_DAYS),
            )
            .order_by(ExchangeRateRow.rate_date.desc())
            .limit(1)
        )
        if not row:
            return None
        return ExchangeRate(float(row["sell"]), row["rate_date"].isoformat())

    def upsert_exchange_rates(self, rates: Iterable[dict[str, Any]]) -> int:
        """Uno cargado a mano o por CSV no se sobrescribe con la descarga de SUNAT."""
        changed = 0
        with self._c() as conn:
            for r in rates:
                day = date.fromisoformat(str(r["date"]))
                values = {"buy": r.get("buy"), "sell": r["sell"], "source": r["source"]}
                key = and_(
                    ExchangeRateRow.currency == r["currency"], ExchangeRateRow.rate_date == day
                )
                # UPDLOCK + HOLDLOCK: dos cargas del mismo día a la vez no chocan.
                current = conn.execute(
                    select(ExchangeRateRow.source)
                    .with_hint(ExchangeRateRow, "WITH (UPDLOCK, HOLDLOCK)", "mssql")
                    .where(key)
                ).scalar()
                if current is None:
                    try:
                        with conn.begin_nested():
                            conn.execute(
                                insert(ExchangeRateRow).values(
                                    currency=r["currency"], rate_date=day, **values
                                )
                            )
                        changed += 1
                    except IntegrityError:
                        pass
                    continue
                if r["source"] == "sunat" and current != "sunat":
                    continue
                result = conn.execute(
                    update(ExchangeRateRow).where(key).values(updated_at=_NOW, **values)
                )
                changed += int(result.rowcount or 0)
        return changed

    def list_exchange_rates(self, *, currency: str = "USD", start: str, end: str) -> list[Row]:
        return self._all(
            select(ExchangeRateRow)
            .where(
                ExchangeRateRow.currency == currency,
                ExchangeRateRow.rate_date >= date.fromisoformat(start),
                ExchangeRateRow.rate_date <= date.fromisoformat(end),
            )
            .order_by(ExchangeRateRow.rate_date)
        )

    def list_invoices_awaiting_exchange_rate(self) -> list[Row]:
        return self._all(
            select(*_INVOICES)
            .where(
                Invoice.exchange_rate.is_(None),
                Invoice.currency.is_not(None),
                Invoice.currency != "PEN",
                Invoice.issue_date.is_not(None),
            )
            .order_by(Invoice.id)
        )

    def set_invoice_exchange_rate(self, invoice_id: int, rate: ExchangeRate | None) -> None:
        self._exec(
            update(Invoice)
            .where(Invoice.id == invoice_id)
            .values(
                exchange_rate=rate.rate if rate else None,
                exchange_rate_date=storable_date(rate.date) if rate else None,
                updated_at=_NOW,
            )
        )

    # --- Correo --------------------------------------------------------------

    def find_mail_messages(self, account: str, message_ids: Sequence[str]) -> dict[str, Row]:
        found: dict[str, Row] = {}
        # SQL Server admite hasta 2100 parámetros por consulta.
        for i in range(0, len(message_ids), 500):
            for row in self._all(
                select(
                    MailMessage.id,
                    MailMessage.message_id,
                    MailMessage.status,
                    MailMessage.attempts,
                    MailMessage.moved_to,
                ).where(
                    MailMessage.account == account,
                    MailMessage.message_id.in_(list(message_ids[i : i + 500])),
                )
            ):
                found[row["message_id"]] = row
        return found

    def record_mail_message(
        self,
        *,
        account: str,
        message_id: str,
        received_at: Any,
        sender: str | None,
        subject: str | None,
        status: str,
        files: int,
        folder: str | None,
        error_message: str | None,
        attachments: Sequence[tuple[str, str]] = (),
    ) -> int:
        """Registra el resultado de leer un mensaje y los archivos que trajo (SHA-256, nombre)."""
        values = {
            "status": status,
            "files": files,
            "folder": folder,
            "sender": sender,
            "subject": subject,
            "received_at": received_at,
            "error_message": error_message,
        }
        with self._c() as conn:
            # UPDLOCK + HOLDLOCK: dos lecturas del mismo mensaje a la vez no lo duplican.
            record_id = conn.execute(
                select(MailMessage.id)
                .with_hint(MailMessage, "WITH (UPDLOCK, HOLDLOCK)", "mssql")
                .where(MailMessage.account == account, MailMessage.message_id == message_id)
            ).scalar()
            if record_id is None:
                record_id = conn.execute(
                    insert(MailMessage)
                    .values(account=account, message_id=message_id, **values)
                    .returning(MailMessage.id)
                ).scalar_one()
            else:
                conn.execute(
                    update(MailMessage)
                    .where(MailMessage.id == record_id)
                    .values(attempts=MailMessage.attempts + 1, updated_at=_NOW, **values)
                )
            if attachments:
                conn.execute(
                    delete(MailAttachment).where(MailAttachment.mail_message_id == record_id)
                )
                # Un mismo archivo puede venir suelto y dentro de un ZIP.
                unique = dict(attachments)
                conn.execute(
                    insert(MailAttachment),
                    [
                        {"mail_message_id": record_id, "sha256": sha, "file_name": name[:260]}
                        for sha, name in unique.items()
                    ],
                )
        return int(record_id)

    def set_mail_message_moved(self, record_id: int, moved_to: str) -> None:
        self._exec(
            update(MailMessage)
            .where(MailMessage.id == record_id)
            .values(moved_to=moved_to, updated_at=_NOW)
        )

    def find_mail_origins(self, sha256: str) -> list[Row]:
        return self._all(
            select(
                MailMessage.account,
                MailMessage.sender,
                MailMessage.subject,
                MailMessage.received_at,
                MailMessage.moved_to,
            )
            .join(MailAttachment, MailAttachment.mail_message_id == MailMessage.id)
            .where(MailAttachment.sha256 == sha256)
            .order_by(MailMessage.received_at)
        )

    def list_mail_messages(self, limit: int = 20) -> list[Row]:
        return self._all(select(MailMessage).order_by(MailMessage.id.desc()).limit(int(limit)))

    # --- Consultas -----------------------------------------------------------

    def _repeated_business_keys(self) -> Any:
        return (
            select(Invoice.business_key)
            .where(Invoice.business_key.is_not(None), Invoice.review_status != "rejected")
            .group_by(Invoice.business_key)
            .having(func.count() > 1)
        )

    def _repeated_files(self) -> Any:
        return (
            select(FileOccurrence.document_id)
            .group_by(FileOccurrence.document_id)
            .having(func.count() > 1)
        )

    def get_status_summary(self) -> dict[str, Any]:
        def count_by(column: Any) -> dict[str, int]:
            rows = self._all(select(column.label("k"), func.count().label("n")).group_by(column))
            return {r["k"]: int(r["n"]) for r in rows}

        def count_of(query: Any) -> Any:
            return select(func.count()).select_from(query.subquery()).scalar_subquery()

        dup = (
            self._one(
                select(
                    count_of(self._repeated_files()).label("exact"),
                    count_of(self._repeated_business_keys()).label("possible"),
                )
            )
            or {}
        )
        runs = (
            self._one(
                select(
                    func.count().label("total"),
                    func.coalesce(
                        func.sum(case((ExtractionRun.status == "failed", 1), else_=0)), 0
                    ).label("failed"),
                    func.coalesce(func.sum(cast(ExtractionRun.input_tokens, BigInteger)), 0).label(
                        "input_tokens"
                    ),
                    func.coalesce(func.sum(cast(ExtractionRun.output_tokens, BigInteger)), 0).label(
                        "output_tokens"
                    ),
                )
            )
            or {}
        )
        return {
            "documents": count_by(Document.status),
            "invoices": count_by(Invoice.validation_status),
            "review": count_by(Invoice.review_status),
            "exact_duplicates": int(dup.get("exact") or 0),
            "possible_duplicates": int(dup.get("possible") or 0),
            "runs": {
                k: int(runs.get(k) or 0)
                for k in ("total", "failed", "input_tokens", "output_tokens")
            },
        }

    def list_exact_duplicates(self) -> list[Row]:
        return self._all(
            select(
                Document.id.label("document_id"),
                Document.file_name,
                Document.sha256,
                FileOccurrence.file_path,
                FileOccurrence.first_seen_at,
            )
            .join(Document, Document.id == FileOccurrence.document_id)
            .where(FileOccurrence.document_id.in_(self._repeated_files()))
            .order_by(Document.id, FileOccurrence.id)
        )

    def list_possible_duplicates(self) -> list[Row]:
        return self._all(
            select(
                Invoice.business_key,
                Invoice.id.label("invoice_id"),
                Invoice.document_id,
                Document.file_path,
                Invoice.supplier_ruc,
                Invoice.document_type,
                Invoice.series,
                Invoice.number,
                Invoice.issue_date,
                Invoice.currency,
                Invoice.total_cents,
            )
            .join(Document, Document.id == Invoice.document_id)
            .where(
                Invoice.review_status != "rejected",
                Invoice.business_key.in_(self._repeated_business_keys()),
            )
            .order_by(Invoice.business_key, Invoice.id)
        )

    def list_invoices_for_export(
        self,
        *,
        month: str | None = None,
        validation_statuses: Sequence[str] = (),
        supplier_ruc: str | None = None,
        currency: str | None = None,
        include_duplicates: bool = False,
    ) -> list[Row]:
        """Por defecto excluye repeticiones de una clave de negocio.

        Conserva la leída de un XML si existe (datos exactos) y, si no, la del
        primer documento registrado.
        """
        other = aliased(Invoice)
        duplicates = (
            select(func.count())
            .where(other.business_key == Invoice.business_key, other.id != Invoice.id)
            .correlate(Invoice)
            .scalar_subquery()
        )
        query = (
            select(
                *_INVOICES,
                Document.file_name,
                Document.file_path,
                Document.sha256,
                duplicates.label("duplicate_count"),
            )
            .join(Document, Document.id == Invoice.document_id)
            .where(Invoice.review_status != "rejected")
            .order_by(
                Invoice.issue_date, Invoice.supplier_ruc, Invoice.series, Invoice.number, Invoice.id
            )
        )
        if month:
            start, end = month_range(month)
            query = query.where(Invoice.issue_date >= start, Invoice.issue_date < end)
        if validation_statuses:
            query = query.where(Invoice.validation_status.in_(list(validation_statuses)))
        if supplier_ruc:
            query = query.where(Invoice.supplier_ruc == supplier_ruc)
        if currency:
            query = query.where(Invoice.currency == currency)
        if not include_duplicates:
            chosen = aliased(Invoice)
            first_of_key = (
                select(chosen.id)
                .where(
                    chosen.business_key == Invoice.business_key,
                    chosen.review_status != "rejected",
                )
                .order_by(case((chosen.data_source == "xml", 0), else_=1), chosen.document_id)
                .limit(1)
                .correlate(Invoice)
                .scalar_subquery()
            )
            query = query.where(or_(Invoice.business_key.is_(None), Invoice.id == first_of_key))
        return self._all(query)

    def list_invoices_needing_review(self) -> list[Row]:
        """Facturas con errores o advertencias sin rechazar."""
        return self._all(
            select(*_LIST_COLUMNS)
            .join(Document, Document.id == Invoice.document_id)
            .where(
                Invoice.validation_status.in_(("error", "warning")),
                Invoice.review_status != "rejected",
            )
            .order_by(case((Invoice.validation_status == "error", 0), else_=1), Invoice.id)
        )

    def search_invoices(
        self,
        *,
        month: str | None = None,
        validation_status: str | None = None,
        review_status: str | None = None,
        supplier_ruc: str | None = None,
        query: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Row], int]:
        """Listado paginado para la API."""
        conditions: list[Any] = []
        if month:
            start, end = month_range(month)
            conditions += [Invoice.issue_date >= start, Invoice.issue_date < end]
        if validation_status:
            conditions.append(Invoice.validation_status == validation_status)
        if review_status:
            conditions.append(Invoice.review_status == review_status)
        if supplier_ruc:
            conditions.append(Invoice.supplier_ruc == supplier_ruc)
        if query:
            escaped = query.replace("[", "[[]").replace("%", "[%]").replace("_", "[_]")
            pattern = f"%{escaped}%"
            conditions.append(
                or_(
                    Invoice.supplier_name.like(pattern),
                    Invoice.number.like(pattern),
                    Invoice.series.like(pattern),
                    Document.file_name.like(pattern),
                    Invoice.supplier_ruc.like(pattern),
                )
            )
        with self._c() as conn:
            total = conn.execute(
                select(func.count())
                .select_from(Invoice)
                .join(Document, Document.id == Invoice.document_id)
                .where(*conditions)
            ).scalar_one()
            rows = _rows(
                conn.execute(
                    select(*_LIST_COLUMNS)
                    .join(Document, Document.id == Invoice.document_id)
                    .where(*conditions)
                    .order_by(Invoice.issue_date.desc(), Invoice.id.desc())
                    .offset(int(offset))
                    .limit(int(limit))
                )
            )
        return rows, int(total or 0)

    # --- Revisión humana -----------------------------------------------------

    def get_invoice_detail(self, invoice_id: int) -> Row | None:
        return self._one(
            select(
                *_INVOICES,
                Document.file_path,
                Document.file_name,
                Document.mime_type,
                Document.sha256,
                Document.page_count,
            )
            .join(Document, Document.id == Invoice.document_id)
            .where(Invoice.id == invoice_id)
        )

    def list_invoices_by_review_status(self, status: str, limit: int = 200) -> list[Row]:
        return self._all(
            select(*_LIST_COLUMNS)
            .join(Document, Document.id == Invoice.document_id)
            .where(Invoice.review_status == status)
            .order_by(Invoice.reviewed_at.desc(), Invoice.id.desc())
            .limit(int(limit))
        )

    def list_review_actions(self, invoice_id: int) -> list[Row]:
        return self._all(
            select(ReviewAction)
            .where(ReviewAction.invoice_id == invoice_id)
            .order_by(ReviewAction.id)
        )

    def record_review_action(
        self,
        *,
        invoice_id: int,
        action: str,
        user: str,
        field: str | None = None,
        old_value: object = None,
        new_value: object = None,
        issue_id: int | None = None,
        note: str | None = None,
    ) -> None:
        self._exec(
            insert(ReviewAction).values(
                invoice_id=invoice_id,
                action=action,
                field=field,
                old_value=_clip(old_value, 1000),
                new_value=_clip(new_value, 1000),
                issue_id=issue_id,
                note=_clip(note, 1000),
                user=user[:100],
            )
        )

    def set_review_status(self, invoice_id: int, status: str, user: str) -> None:
        reviewed: dict[str, Any] = (
            {"reviewed_at": None, "reviewed_by": None}
            if status == "pending"
            else {"reviewed_at": _NOW, "reviewed_by": user[:100]}
        )
        self._exec(
            update(Invoice)
            .where(Invoice.id == invoice_id)
            .values(review_status=status, updated_at=_NOW, **reviewed)
        )

    def resolve_open_issues(
        self, invoice_id: int, *, severities: Sequence[str], user: str, note: str | None = None
    ) -> int:
        return self._exec(
            update(ValidationIssue)
            .where(*_open_issues(invoice_id), ValidationIssue.severity.in_(list(severities)))
            .values(resolved_at=_NOW, resolved_by=user[:100], resolution_note=_clip(note, 1000))
        )

    def get_issue(self, issue_id: int) -> Row | None:
        return self._one(select(ValidationIssue).where(ValidationIssue.id == issue_id))

    def resolve_issue(self, issue_id: int, *, user: str, note: str | None = None) -> None:
        self._exec(
            update(ValidationIssue)
            .where(ValidationIssue.id == issue_id)
            .values(resolved_at=_NOW, resolved_by=user[:100], resolution_note=_clip(note, 1000))
        )

    def reopen_issues(self, invoice_id: int) -> int:
        return self._exec(
            update(ValidationIssue)
            .where(
                ValidationIssue.invoice_id == invoice_id,
                ValidationIssue.resolved_at.is_not(None),
            )
            .values(resolved_at=None, resolved_by=None, resolution_note=None)
        )

    def update_invoice_fields(self, invoice_id: int, changes: dict[str, Any]) -> None:
        """Aplica correcciones manuales ({columna: valor}) y marca la factura como corregida."""
        values = {c: v for c, v in changes.items() if c in INVOICE_COLUMNS}
        if not values:
            return
        for column in ("issue_date", "due_date"):
            if column in values:
                values[column] = storable_date(values[column])
        self._exec(
            update(Invoice)
            .where(Invoice.id == invoice_id)
            .values(corrected=True, updated_at=_NOW, **values)
        )

    def clear_sunat_status(self, invoice_id: int) -> None:
        self._exec(
            update(Invoice)
            .where(Invoice.id == invoice_id)
            .values(sunat_status=None, sunat_checked_at=None)
        )

    def set_business_key(self, invoice_id: int, key: str | None) -> None:
        self._exec(update(Invoice).where(Invoice.id == invoice_id).values(business_key=key))

    def replace_rule_issues(
        self, invoice_id: int, issues: Sequence[NewIssue], is_preserved: Callable[[str], bool]
    ) -> None:
        """Reemplaza las incidencias abiertas de las reglas; las preservadas se mantienen."""
        open_rows = self._all(
            select(ValidationIssue.id, ValidationIssue.rule_code).where(*_open_issues(invoice_id))
        )
        remove = [r["id"] for r in open_rows if not is_preserved(r["rule_code"])]
        if remove:
            self._exec(delete(ValidationIssue).where(ValidationIssue.id.in_(remove)))
        self.insert_issues(invoice_id, issues)

    def recompute_validation_status(self, invoice_id: int) -> str:
        open_rows = self._all(select(ValidationIssue.severity).where(*_open_issues(invoice_id)))
        status = status_from_issues(open_rows)
        self._exec(
            update(Invoice)
            .where(Invoice.id == invoice_id)
            .values(validation_status=status, updated_at=_NOW)
        )
        return status

    # --- Ejecuciones desatendidas --------------------------------------------

    def start_bot_run(self, *, trigger: str, host: str | None, pid: int | None) -> int:
        row = self._one(
            insert(BotRun)
            .values(trigger=trigger, status="running", host=host, pid=pid)
            .returning(BotRun.id)
        )
        assert row is not None
        return int(row["id"])

    def finish_bot_run(
        self,
        run_id: int,
        *,
        status: str,
        exit_code: int,
        summary: Any = None,
        error_message: str | None = None,
    ) -> None:
        self._exec(
            update(BotRun)
            .where(BotRun.id == run_id)
            .values(
                status=status,
                exit_code=exit_code,
                summary=json.dumps(summary, ensure_ascii=False, default=str) if summary else None,
                error_message=_clip(error_message, 4000),
                finished_at=_NOW,
            )
        )

    def close_abandoned_bot_runs(self) -> int:
        """Ejecuciones que quedaron "running" porque el proceso murió sin cerrarlas."""
        return self._exec(
            update(BotRun)
            .where(BotRun.status == "running")
            .values(
                status="failed",
                error_message="La ejecución terminó de forma inesperada",
                finished_at=_NOW,
            )
        )

    def list_bot_runs(self, limit: int = 10) -> list[Row]:
        rows = self._all(select(BotRun).order_by(BotRun.id.desc()).limit(int(limit)))
        for row in rows:
            row["summary"] = json.loads(row["summary"]) if row.get("summary") else None
        return rows

    def get_bot_run(self, run_id: int) -> Row | None:
        row = self._one(select(BotRun).where(BotRun.id == run_id))
        if row and row.get("summary"):
            row["summary"] = json.loads(row["summary"])
        return row

    # --- SUNAT ---------------------------------------------------------------

    def list_invoices_for_sunat_check(
        self,
        *,
        document_types: Sequence[str],
        month: str | None = None,
        force: bool = False,
        limit: int | None = None,
    ) -> list[Row]:
        """Sin `force`: las nunca consultadas con éxito y las que SUNAT aún no tenía."""
        query = (
            select(*_INVOICES)
            .where(
                Invoice.supplier_ruc.is_not(None),
                Invoice.series.is_not(None),
                Invoice.number.is_not(None),
                Invoice.issue_date.is_not(None),
                Invoice.document_type.in_(list(document_types)),
            )
            .order_by(Invoice.issue_date, Invoice.id)
        )
        if month:
            start, end = month_range(month)
            query = query.where(Invoice.issue_date >= start, Invoice.issue_date < end)
        if not force:
            query = query.where(
                or_(Invoice.sunat_checked_at.is_(None), Invoice.sunat_status == "NO EXISTE")
            )
        if limit:
            query = query.limit(int(limit))
        return self._all(query)

    def record_sunat_check(
        self,
        *,
        invoice_id: int,
        result: SunatResult | None = None,
        error: tuple[str, str] | None = None,
        receipt_status: str | None = None,
        issues: Sequence[NewIssue] = (),
    ) -> str | None:
        """Registra una consulta; si fue correcta reemplaza las incidencias SUNAT_*."""
        with self.transaction() as repo:
            repo._exec(
                insert(SunatCheck).values(
                    invoice_id=invoice_id,
                    status="failed" if error else "success",
                    estado_cp=result.estado_cp if result else None,
                    estado_ruc=result.estado_ruc if result else None,
                    cond_domi_ruc=result.cond_domi_ruc if result else None,
                    observations=(
                        json.dumps(result.observaciones, ensure_ascii=False) if result else None
                    ),
                    error_kind=error[0] if error else None,
                    error_message=_clip(error[1], 4000) if error else None,
                    raw_response=(
                        json.dumps(result.raw, ensure_ascii=False, default=str) if result else None
                    ),
                )
            )
            if error:
                return None
            repo._exec(
                delete(ValidationIssue).where(
                    ValidationIssue.invoice_id == invoice_id,
                    ValidationIssue.rule_code.startswith(SUNAT_RULE_PREFIX, autoescape=True),
                )
            )
            repo.insert_issues(invoice_id, issues)
            open_rows = repo._all(select(ValidationIssue.severity).where(*_open_issues(invoice_id)))
            status = status_from_issues(open_rows)
            repo._exec(
                update(Invoice)
                .where(Invoice.id == invoice_id)
                .values(
                    sunat_status=receipt_status,
                    sunat_checked_at=_NOW,
                    validation_status=status,
                    updated_at=_NOW,
                )
            )
            return status
