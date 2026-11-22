"""Repositorio SQL Server de documentos, extracciones, facturas e incidencias.

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
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, bindparam, text

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

Row = dict[str, Any]

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

_LIST_COLUMNS = """
    i.id AS invoice_id, i.document_id, i.validation_status, i.review_status, i.supplier_name,
    i.supplier_ruc, i.document_type, i.series, i.number, i.issue_date, i.currency, i.total_cents,
    i.data_source, i.sunat_status, i.reviewed_by, i.reviewed_at, d.file_name, d.file_path
"""


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
    return [as_row(r._mapping) for r in result]


def _first(result: Any) -> Row | None:
    row = result.first()
    return as_row(row._mapping) if row is not None else None


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

    def _all(self, sql: str, params: dict[str, Any] | None = None, **bind: Any) -> list[Row]:
        statement = text(sql)
        for name in bind.get("expanding", ()):
            statement = statement.bindparams(bindparam(name, expanding=True))
        with self._c() as conn:
            return _rows(conn.execute(statement, params or {}))

    def _one(self, sql: str, params: dict[str, Any] | None = None) -> Row | None:
        with self._c() as conn:
            return _first(conn.execute(text(sql), params or {}))

    def _exec(self, sql: str, params: dict[str, Any] | list[dict[str, Any]] | None = None) -> int:
        with self._c() as conn:
            return int(conn.execute(text(sql), params or {}).rowcount or 0)

    # --- Documentos ---------------------------------------------------------

    def find_document_by_sha(self, sha256: str) -> Row | None:
        return self._one("SELECT * FROM documents WHERE sha256 = :sha", {"sha": sha256})

    def get_document(self, document_id: int) -> Row | None:
        return self._one("SELECT * FROM documents WHERE id = :id", {"id": document_id})

    def find_document_by_path(self, file_path: str) -> Row | None:
        """Documento visto por última vez en `file_path` (según file_occurrences)."""
        return self._one(
            """SELECT d.* FROM file_occurrences o JOIN documents d ON d.id = o.document_id
               WHERE o.path_hash = HASHBYTES('SHA2_256', CAST(:p AS NVARCHAR(1024)))
                 AND o.file_path = :p""",
            {"p": file_path},
        )

    def insert_document(
        self, *, sha256: str, file_path: str, file_name: str, mime_type: str, file_size: int
    ) -> Row:
        row = self._one(
            """INSERT INTO documents (sha256, file_path, file_name, mime_type, file_size, status)
               OUTPUT inserted.*
               VALUES (:sha, :path, :name, :mime, :size, N'pending')""",
            {
                "sha": sha256,
                "path": file_path,
                "name": file_name[:260],
                "mime": mime_type,
                "size": file_size,
            },
        )
        assert row is not None
        return row

    def record_occurrence(self, document_id: int, file_path: str) -> bool:
        """Registra que el documento se vio en `file_path`. True si la ruta es nueva para él."""
        with self._c() as conn:
            existing = _first(
                conn.execute(
                    text(
                        """SELECT id, document_id FROM file_occurrences
                           WHERE path_hash = HASHBYTES('SHA2_256', CAST(:p AS NVARCHAR(1024)))
                             AND file_path = :p"""
                    ),
                    {"p": file_path},
                )
            )
            if existing:
                # El archivo de esa ruta pudo cambiar de contenido: se reasigna.
                conn.execute(
                    text(
                        "UPDATE file_occurrences SET document_id = :d, "
                        "last_seen_at = SYSUTCDATETIME() WHERE id = :id"
                    ),
                    {"d": document_id, "id": existing["id"]},
                )
                return bool(existing["document_id"] != document_id)
            conn.execute(
                text("INSERT INTO file_occurrences (document_id, file_path) VALUES (:d, :p)"),
                {"d": document_id, "p": file_path},
            )
            return True

    def get_occurrences(self, document_id: int) -> list[Row]:
        return self._all(
            "SELECT file_path, first_seen_at, last_seen_at FROM file_occurrences "
            "WHERE document_id = :d ORDER BY id",
            {"d": document_id},
        )

    def start_attempt(self, document_id: int) -> int:
        """Marca el documento en proceso e incrementa su contador de intentos."""
        row = self._one(
            """UPDATE documents SET status = N'processing', attempts = attempts + 1,
                 error_kind = NULL, error_message = NULL, updated_at = SYSUTCDATETIME()
               OUTPUT inserted.attempts WHERE id = :id""",
            {"id": document_id},
        )
        assert row is not None
        return int(row["attempts"])

    def update_document(self, document_id: int, *, processed: bool = False, **fields: Any) -> None:
        unknown = set(fields) - _DOCUMENT_FIELDS
        if unknown:
            raise ValueError(f"Columnas de documento desconocidas: {sorted(unknown)}")
        if "error_message" in fields:
            fields["error_message"] = _clip(fields["error_message"], 4000)
        sets = [f"{name} = :{name}" for name in fields]
        if processed:
            sets.append("processed_at = SYSUTCDATETIME()")
        if not sets:
            return
        sets.append("updated_at = SYSUTCDATETIME()")
        self._exec(
            f"UPDATE documents SET {', '.join(sets)} WHERE id = :_id",
            {**fields, "_id": document_id},
        )

    def reset_interrupted(self) -> int:
        """Documentos que quedaron "en proceso" por una ejecución interrumpida."""
        return self._exec(
            "UPDATE documents SET status = N'pending', updated_at = SYSUTCDATETIME() "
            "WHERE status = N'processing'"
        )

    def list_documents_by_status(self, statuses: Sequence[str]) -> list[Row]:
        if not statuses:
            return []
        return self._all(
            "SELECT * FROM documents WHERE status IN :statuses ORDER BY id",
            {"statuses": list(statuses)},
            expanding=("statuses",),
        )

    # --- Extracciones --------------------------------------------------------

    def insert_extraction_run(self, **run: Any) -> int:
        values = {
            "document_id": run["document_id"],
            "attempt": run["attempt"],
            "provider": run["provider"],
            "model": run.get("model"),
            "prompt_version": run["prompt_version"],
            "text_method": run.get("text_method"),
            "input_chars": run.get("input_chars"),
            "status": run["status"],
            "error_kind": run.get("error_kind"),
            "error_message": _clip(run.get("error_message"), 4000),
            "raw_response": run.get("raw_response"),
            "input_tokens": run.get("input_tokens"),
            "output_tokens": run.get("output_tokens"),
            "duration_ms": run.get("duration_ms"),
        }
        row = self._one(
            """INSERT INTO extraction_runs (document_id, attempt, provider, model, prompt_version,
                 text_method, input_chars, status, error_kind, error_message, raw_response,
                 input_tokens, output_tokens, duration_ms)
               OUTPUT inserted.id
               VALUES (:document_id, :attempt, :provider, :model, :prompt_version, :text_method,
                 :input_chars, :status, :error_kind, :error_message, :raw_response,
                 :input_tokens, :output_tokens, :duration_ms)""",
            values,
        )
        assert row is not None
        return int(row["id"])

    def list_extraction_runs(self, document_id: int) -> list[Row]:
        return self._all(
            "SELECT * FROM extraction_runs WHERE document_id = :d ORDER BY id", {"d": document_id}
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
        data_columns = [*INVOICE_COLUMNS, "exchange_rate", "exchange_rate_date", "company_id"]
        with self.transaction() as repo, repo._c() as conn:
            existing = conn.execute(
                text("SELECT id FROM invoices WHERE document_id = :d"), {"d": document_id}
            ).scalar()
            if existing:
                sets = ", ".join(f"{c} = :{c}" for c in data_columns)
                conn.execute(
                    text(
                        f"""UPDATE invoices SET {sets}, data_source = :data_source,
                              extraction_run_id = :extraction_run_id, business_key = :business_key,
                              validation_status = :validation_status, rules_version = :rules_version,
                              updated_at = SYSUTCDATETIME(), sunat_status = NULL,
                              sunat_checked_at = NULL, review_status = N'pending',
                              reviewed_at = NULL, reviewed_by = NULL, corrected = 0
                            WHERE id = :_id"""
                    ),
                    {**values, "_id": existing},
                )
                conn.execute(
                    text("DELETE FROM validation_issues WHERE invoice_id = :i"), {"i": existing}
                )
                invoice_id = int(existing)
            else:
                columns = [
                    *data_columns,
                    "document_id",
                    "extraction_run_id",
                    "data_source",
                    "business_key",
                    "validation_status",
                    "rules_version",
                ]
                invoice_id = int(
                    conn.execute(
                        # `invoices` tiene un trigger de auditoría: OUTPUT necesita INTO.
                        text(
                            "SET NOCOUNT ON; DECLARE @ids TABLE (id INT); "
                            f"INSERT INTO invoices ({', '.join(columns)}) OUTPUT inserted.id INTO @ids "
                            f"VALUES ({', '.join(':' + c for c in columns)}); SELECT id FROM @ids"
                        ),
                        values,
                    ).scalar_one()
                )
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
        row = self._one(
            """SELECT (SELECT TOP (1) id FROM companies WHERE ruc = :r AND active = 1) AS id,
                      (SELECT COUNT(*) FROM companies WHERE active = 1) AS n""",
            {"r": customer_ruc or ""},
        )
        if row is None:
            return None, False
        return (int(row["id"]) if row["id"] is not None else None), int(row["n"] or 0) > 0

    def invoice_history(self, invoice_id: int) -> list[Row]:
        """Cada estado de la factura (trigger de auditoría): qué cambió, cuándo y quién.

        Respeta la Row-Level Security: solo se muestra si la factura es visible.
        """
        return self._all(
            """SELECT c.id, c.operation, c.changed_at, c.changed_by, c.validation_status,
                      c.review_status, c.reviewed_by, c.supplier_ruc, c.document_type, c.series,
                      c.number, c.issue_date, c.currency, c.total_cents, c.igv_cents,
                      c.sunat_status, c.company_id, c.corrected
               FROM invoice_changes c
               WHERE c.invoice_id = :id AND EXISTS (SELECT 1 FROM invoices i WHERE i.id = :id)
               ORDER BY c.id""",
            {"id": invoice_id},
        )

    def insert_issues(self, invoice_id: int, issues: Sequence[NewIssue]) -> None:
        if not issues:
            return
        params = [
            {
                "i": invoice_id,
                "r": issue.rule_code,
                "s": issue.severity,
                "m": issue.message[:1000],
                "e": _clip(issue.expected_value, 1000),
                "a": _clip(issue.actual_value, 1000),
            }
            for issue in issues
        ]
        self._exec(
            """INSERT INTO validation_issues
                 (invoice_id, rule_code, severity, message, expected_value, actual_value)
               VALUES (:i, :r, :s, :m, :e, :a)""",
            params,
        )

    def get_invoice_by_document(self, document_id: int) -> Row | None:
        return self._one("SELECT * FROM invoices WHERE document_id = :d", {"d": document_id})

    def get_issues(self, invoice_id: int) -> list[Row]:
        return self._all(
            "SELECT * FROM validation_issues WHERE invoice_id = :i ORDER BY id", {"i": invoice_id}
        )

    def lock_business_key(self, key: str) -> None:
        """Reserva la clave de negocio hasta el final de la transacción en curso."""
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
            """SELECT i.id, i.document_id, d.file_name, d.file_path
               FROM invoices i JOIN documents d ON d.id = i.document_id
               WHERE i.business_key = :k AND i.document_id <> :d AND i.review_status <> N'rejected'
               ORDER BY i.id""",
            {"k": key, "d": exclude_document_id},
        )

    # --- Tipo de cambio -----------------------------------------------------

    def find_exchange_rate(self, currency: str, on: str) -> ExchangeRate | None:
        """Tipo de cambio venta de `currency` para `on`, o el último publicado antes."""
        row = self._one(
            """SELECT TOP (1) sell, rate_date FROM exchange_rates
               WHERE currency = :c AND rate_date <= CAST(:d AS DATE)
                 AND rate_date >= DATEADD(DAY, :back, CAST(:d AS DATE))
               ORDER BY rate_date DESC""",
            {"c": currency, "d": on, "back": -EXCHANGE_RATE_LOOKBACK_DAYS},
        )
        if not row:
            return None
        return ExchangeRate(float(row["sell"]), row["rate_date"].isoformat())

    def upsert_exchange_rates(self, rates: Iterable[dict[str, Any]]) -> int:
        """Uno cargado a mano o por CSV no se sobrescribe con la descarga de SUNAT."""
        changed = 0
        with self._c() as conn:
            for r in rates:
                changed += int(
                    conn.execute(
                        text(
                            """MERGE exchange_rates AS t
                               USING (SELECT CAST(:c AS NVARCHAR(3)) AS currency,
                                             CAST(:d AS DATE) AS rate_date) AS s
                                 ON t.currency = s.currency AND t.rate_date = s.rate_date
                               WHEN MATCHED AND NOT (:src = N'sunat' AND t.source <> N'sunat') THEN
                                 UPDATE SET buy = :buy, sell = :sell, source = :src,
                                   updated_at = SYSUTCDATETIME()
                               WHEN NOT MATCHED THEN
                                 INSERT (currency, rate_date, buy, sell, source)
                                 VALUES (:c, CAST(:d AS DATE), :buy, :sell, :src);"""
                        ),
                        {
                            "c": r["currency"],
                            "d": r["date"],
                            "buy": r.get("buy"),
                            "sell": r["sell"],
                            "src": r["source"],
                        },
                    ).rowcount
                    or 0
                )
        return changed

    def list_exchange_rates(self, *, currency: str = "USD", start: str, end: str) -> list[Row]:
        return self._all(
            """SELECT * FROM exchange_rates WHERE currency = :c
                 AND rate_date >= CAST(:a AS DATE) AND rate_date <= CAST(:b AS DATE)
               ORDER BY rate_date""",
            {"c": currency, "a": start, "b": end},
        )

    def list_invoices_awaiting_exchange_rate(self) -> list[Row]:
        return self._all(
            """SELECT * FROM invoices WHERE exchange_rate IS NULL AND currency IS NOT NULL
                 AND currency <> N'PEN' AND issue_date IS NOT NULL ORDER BY id"""
        )

    def set_invoice_exchange_rate(self, invoice_id: int, rate: ExchangeRate | None) -> None:
        self._exec(
            """UPDATE invoices SET exchange_rate = :r, exchange_rate_date = :d,
                 updated_at = SYSUTCDATETIME() WHERE id = :id""",
            {
                "r": rate.rate if rate else None,
                "d": storable_date(rate.date) if rate else None,
                "id": invoice_id,
            },
        )

    # --- Correo --------------------------------------------------------------

    def find_mail_messages(self, account: str, message_ids: Sequence[str]) -> dict[str, Row]:
        found: dict[str, Row] = {}
        # SQL Server admite hasta 2100 parámetros por consulta.
        for i in range(0, len(message_ids), 500):
            for row in self._all(
                """SELECT id, message_id, status, attempts, moved_to FROM mail_messages
                   WHERE account = :a AND message_id IN :ids""",
                {"a": account, "ids": list(message_ids[i : i + 500])},
                expanding=("ids",),
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
        params = {
            "a": account,
            "m": message_id,
            "r": received_at,
            "s": sender,
            "sub": subject,
            "st": status,
            "f": files,
            "fo": folder,
            "e": error_message,
        }
        with self._c() as conn:
            record_id = int(
                conn.execute(
                    text(
                        """SET NOCOUNT ON;
                           MERGE mail_messages WITH (HOLDLOCK) AS t
                           USING (SELECT CAST(:a AS NVARCHAR(320)) AS account,
                                         CAST(:m AS NVARCHAR(400)) AS message_id) AS s
                             ON t.account = s.account AND t.message_id = s.message_id
                           WHEN MATCHED THEN
                             UPDATE SET status = :st, files = :f, folder = :fo, sender = :s,
                               subject = :sub, received_at = :r, error_message = :e,
                               attempts = t.attempts + 1, updated_at = SYSUTCDATETIME()
                           WHEN NOT MATCHED THEN
                             INSERT (account, message_id, received_at, sender, subject, status,
                                     files, folder, error_message)
                             VALUES (:a, :m, :r, :s, :sub, :st, :f, :fo, :e)
                           OUTPUT inserted.id;"""
                    ),
                    params,
                ).scalar_one()
            )
            if attachments:
                conn.execute(
                    text("DELETE FROM mail_attachments WHERE mail_message_id = :id"),
                    {"id": record_id},
                )
                # Un mismo archivo puede venir suelto y dentro de un ZIP.
                unique = dict(attachments)
                conn.execute(
                    text(
                        "INSERT INTO mail_attachments (mail_message_id, sha256, file_name) "
                        "VALUES (:id, :sha, :name)"
                    ),
                    [
                        {"id": record_id, "sha": sha, "name": name[:260]}
                        for sha, name in unique.items()
                    ],
                )
        return record_id

    def set_mail_message_moved(self, record_id: int, moved_to: str) -> None:
        self._exec(
            "UPDATE mail_messages SET moved_to = :m, updated_at = SYSUTCDATETIME() WHERE id = :id",
            {"m": moved_to, "id": record_id},
        )

    def find_mail_origins(self, sha256: str) -> list[Row]:
        return self._all(
            """SELECT m.account, m.sender, m.subject, m.received_at, m.moved_to
               FROM mail_attachments a JOIN mail_messages m ON m.id = a.mail_message_id
               WHERE a.sha256 = :sha ORDER BY m.received_at""",
            {"sha": sha256},
        )

    def list_mail_messages(self, limit: int = 20) -> list[Row]:
        return self._all("SELECT TOP (:n) * FROM mail_messages ORDER BY id DESC", {"n": int(limit)})

    # --- Consultas -----------------------------------------------------------

    def get_status_summary(self) -> dict[str, Any]:
        def count_by(sql: str) -> dict[str, int]:
            return {r["k"]: int(r["n"]) for r in self._all(sql)}

        dup = (
            self._one(
                """SELECT
                 (SELECT COUNT(*) FROM (SELECT document_id FROM file_occurrences
                    GROUP BY document_id HAVING COUNT(*) > 1) AS e) AS exact_count,
                 (SELECT COUNT(*) FROM (SELECT business_key FROM invoices
                    WHERE business_key IS NOT NULL AND review_status <> N'rejected'
                    GROUP BY business_key HAVING COUNT(*) > 1) AS p) AS possible_count"""
            )
            or {}
        )
        runs = (
            self._one(
                """SELECT COUNT(*) AS total,
                 COALESCE(SUM(CASE WHEN status = N'failed' THEN 1 ELSE 0 END), 0) AS failed,
                 COALESCE(SUM(CAST(input_tokens AS BIGINT)), 0) AS input_tokens,
                 COALESCE(SUM(CAST(output_tokens AS BIGINT)), 0) AS output_tokens
               FROM extraction_runs"""
            )
            or {}
        )
        return {
            "documents": count_by(
                "SELECT status AS k, COUNT(*) AS n FROM documents GROUP BY status"
            ),
            "invoices": count_by(
                "SELECT validation_status AS k, COUNT(*) AS n FROM invoices GROUP BY validation_status"
            ),
            "review": count_by(
                "SELECT review_status AS k, COUNT(*) AS n FROM invoices GROUP BY review_status"
            ),
            "exact_duplicates": int(dup.get("exact_count") or 0),
            "possible_duplicates": int(dup.get("possible_count") or 0),
            "runs": {
                k: int(runs.get(k) or 0)
                for k in ("total", "failed", "input_tokens", "output_tokens")
            },
        }

    def list_exact_duplicates(self) -> list[Row]:
        return self._all(
            """SELECT d.id AS document_id, d.file_name, d.sha256, o.file_path, o.first_seen_at
               FROM file_occurrences o JOIN documents d ON d.id = o.document_id
               WHERE o.document_id IN (SELECT document_id FROM file_occurrences
                                       GROUP BY document_id HAVING COUNT(*) > 1)
               ORDER BY d.id, o.id"""
        )

    def list_possible_duplicates(self) -> list[Row]:
        return self._all(
            """SELECT i.business_key, i.id AS invoice_id, i.document_id, d.file_path,
                 i.supplier_ruc, i.document_type, i.series, i.number, i.issue_date, i.currency,
                 i.total_cents
               FROM invoices i JOIN documents d ON d.id = i.document_id
               WHERE i.review_status <> N'rejected'
                 AND i.business_key IN (SELECT business_key FROM invoices
                     WHERE business_key IS NOT NULL AND review_status <> N'rejected'
                     GROUP BY business_key HAVING COUNT(*) > 1)
               ORDER BY i.business_key, i.id"""
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
        where = ["i.review_status <> N'rejected'"]
        params: dict[str, Any] = {}
        expanding: tuple[str, ...] = ()
        if month:
            params["from"], params["to"] = month_range(month)
            where.append("i.issue_date >= :from AND i.issue_date < :to")
        if validation_statuses:
            where.append("i.validation_status IN :statuses")
            params["statuses"] = list(validation_statuses)
            expanding = ("statuses",)
        if supplier_ruc:
            where.append("i.supplier_ruc = :ruc")
            params["ruc"] = supplier_ruc
        if currency:
            where.append("i.currency = :cur")
            params["cur"] = currency
        if not include_duplicates:
            where.append(
                """(i.business_key IS NULL OR i.id = (SELECT TOP (1) i2.id FROM invoices i2
                     WHERE i2.business_key = i.business_key AND i2.review_status <> N'rejected'
                     ORDER BY CASE i2.data_source WHEN N'xml' THEN 0 ELSE 1 END, i2.document_id))"""
            )
        return self._all(
            f"""SELECT i.*, d.file_name, d.file_path, d.sha256,
                  (SELECT COUNT(*) FROM invoices i3
                   WHERE i3.business_key = i.business_key AND i3.id <> i.id) AS duplicate_count
                FROM invoices i JOIN documents d ON d.id = i.document_id
                WHERE {" AND ".join(where)}
                ORDER BY i.issue_date, i.supplier_ruc, i.series, i.number, i.id""",
            params,
            expanding=expanding,
        )

    def list_invoices_needing_review(self) -> list[Row]:
        """Facturas con errores o advertencias sin rechazar."""
        return self._all(
            f"""SELECT {_LIST_COLUMNS}
                FROM invoices i JOIN documents d ON d.id = i.document_id
                WHERE i.validation_status IN (N'error', N'warning') AND i.review_status <> N'rejected'
                ORDER BY CASE i.validation_status WHEN N'error' THEN 0 ELSE 1 END, i.id"""
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
        where = ["1 = 1"]
        params: dict[str, Any] = {"limit": int(limit), "offset": int(offset)}
        if month:
            params["from"], params["to"] = month_range(month)
            where.append("i.issue_date >= :from AND i.issue_date < :to")
        if validation_status:
            where.append("i.validation_status = :vs")
            params["vs"] = validation_status
        if review_status:
            where.append("i.review_status = :rs")
            params["rs"] = review_status
        if supplier_ruc:
            where.append("i.supplier_ruc = :ruc")
            params["ruc"] = supplier_ruc
        if query:
            where.append(
                "(i.supplier_name LIKE :q OR i.number LIKE :q OR i.series LIKE :q "
                "OR d.file_name LIKE :q OR i.supplier_ruc LIKE :q)"
            )
            escaped = query.replace("[", "[[]").replace("%", "[%]").replace("_", "[_]")
            params["q"] = f"%{escaped}%"
        condition = " AND ".join(where)
        total = self._one(
            f"SELECT COUNT(*) AS n FROM invoices i JOIN documents d ON d.id = i.document_id "
            f"WHERE {condition}",
            params,
        )
        rows = self._all(
            f"""SELECT {_LIST_COLUMNS}
                FROM invoices i JOIN documents d ON d.id = i.document_id
                WHERE {condition}
                ORDER BY i.issue_date DESC, i.id DESC
                OFFSET :offset ROWS FETCH NEXT :limit ROWS ONLY""",
            params,
        )
        return rows, int((total or {}).get("n") or 0)

    # --- Revisión humana -----------------------------------------------------

    def get_invoice_detail(self, invoice_id: int) -> Row | None:
        return self._one(
            """SELECT i.*, d.file_path, d.file_name, d.mime_type, d.sha256, d.page_count
               FROM invoices i JOIN documents d ON d.id = i.document_id WHERE i.id = :id""",
            {"id": invoice_id},
        )

    def list_invoices_by_review_status(self, status: str, limit: int = 200) -> list[Row]:
        return self._all(
            f"""SELECT TOP (:n) {_LIST_COLUMNS}
                FROM invoices i JOIN documents d ON d.id = i.document_id
                WHERE i.review_status = :s ORDER BY i.reviewed_at DESC, i.id DESC""",
            {"n": int(limit), "s": status},
        )

    def list_review_actions(self, invoice_id: int) -> list[Row]:
        return self._all(
            "SELECT * FROM review_actions WHERE invoice_id = :i ORDER BY id", {"i": invoice_id}
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
            """INSERT INTO review_actions
                 (invoice_id, action, field, old_value, new_value, issue_id, note, [user])
               VALUES (:i, :a, :f, :o, :n, :iss, :note, :u)""",
            {
                "i": invoice_id,
                "a": action,
                "f": field,
                "o": _clip(old_value, 1000),
                "n": _clip(new_value, 1000),
                "iss": issue_id,
                "note": _clip(note, 1000),
                "u": user[:100],
            },
        )

    def set_review_status(self, invoice_id: int, status: str, user: str) -> None:
        if status == "pending":
            sql = """UPDATE invoices SET review_status = :s, reviewed_at = NULL, reviewed_by = NULL,
                       updated_at = SYSUTCDATETIME() WHERE id = :id"""
        else:
            sql = """UPDATE invoices SET review_status = :s, reviewed_at = SYSUTCDATETIME(),
                       reviewed_by = :u, updated_at = SYSUTCDATETIME() WHERE id = :id"""
        self._exec(sql, {"s": status, "u": user[:100], "id": invoice_id})

    def resolve_open_issues(
        self, invoice_id: int, *, severities: Sequence[str], user: str, note: str | None = None
    ) -> int:
        statement = text(
            """UPDATE validation_issues SET resolved_at = SYSUTCDATETIME(), resolved_by = :u,
                 resolution_note = :n
               WHERE invoice_id = :i AND resolved_at IS NULL AND severity IN :sev"""
        ).bindparams(bindparam("sev", expanding=True))
        with self._c() as conn:
            return int(
                conn.execute(
                    statement,
                    {
                        "u": user[:100],
                        "n": _clip(note, 1000),
                        "i": invoice_id,
                        "sev": list(severities),
                    },
                ).rowcount
                or 0
            )

    def get_issue(self, issue_id: int) -> Row | None:
        return self._one("SELECT * FROM validation_issues WHERE id = :id", {"id": issue_id})

    def resolve_issue(self, issue_id: int, *, user: str, note: str | None = None) -> None:
        self._exec(
            """UPDATE validation_issues SET resolved_at = SYSUTCDATETIME(), resolved_by = :u,
                 resolution_note = :n WHERE id = :id""",
            {"u": user[:100], "n": _clip(note, 1000), "id": issue_id},
        )

    def reopen_issues(self, invoice_id: int) -> int:
        return self._exec(
            """UPDATE validation_issues SET resolved_at = NULL, resolved_by = NULL,
                 resolution_note = NULL WHERE invoice_id = :i AND resolved_at IS NOT NULL""",
            {"i": invoice_id},
        )

    def update_invoice_fields(self, invoice_id: int, changes: dict[str, Any]) -> None:
        """Aplica correcciones manuales ({columna: valor}) y marca la factura como corregida."""
        values = {c: v for c, v in changes.items() if c in INVOICE_COLUMNS}
        if not values:
            return
        for column in ("issue_date", "due_date"):
            if column in values:
                values[column] = storable_date(values[column])
        sets = ", ".join(f"{c} = :{c}" for c in values)
        self._exec(
            f"UPDATE invoices SET {sets}, corrected = 1, updated_at = SYSUTCDATETIME() WHERE id = :_id",
            {**values, "_id": invoice_id},
        )

    def clear_sunat_status(self, invoice_id: int) -> None:
        self._exec(
            "UPDATE invoices SET sunat_status = NULL, sunat_checked_at = NULL WHERE id = :id",
            {"id": invoice_id},
        )

    def set_business_key(self, invoice_id: int, key: str | None) -> None:
        self._exec(
            "UPDATE invoices SET business_key = :k WHERE id = :id", {"k": key, "id": invoice_id}
        )

    def replace_rule_issues(
        self, invoice_id: int, issues: Sequence[NewIssue], is_preserved: Callable[[str], bool]
    ) -> None:
        """Reemplaza las incidencias abiertas de las reglas; las preservadas se mantienen."""
        open_rows = self._all(
            "SELECT id, rule_code FROM validation_issues WHERE invoice_id = :i AND resolved_at IS NULL",
            {"i": invoice_id},
        )
        remove = [r["id"] for r in open_rows if not is_preserved(r["rule_code"])]
        if remove:
            statement = text("DELETE FROM validation_issues WHERE id IN :ids").bindparams(
                bindparam("ids", expanding=True)
            )
            with self._c() as conn:
                conn.execute(statement, {"ids": remove})
        self.insert_issues(invoice_id, issues)

    def recompute_validation_status(self, invoice_id: int) -> str:
        open_rows = self._all(
            "SELECT severity FROM validation_issues WHERE invoice_id = :i AND resolved_at IS NULL",
            {"i": invoice_id},
        )
        status = status_from_issues(open_rows)
        self._exec(
            "UPDATE invoices SET validation_status = :s, updated_at = SYSUTCDATETIME() WHERE id = :i",
            {"s": status, "i": invoice_id},
        )
        return status

    # --- Ejecuciones desatendidas --------------------------------------------

    def start_bot_run(self, *, trigger: str, host: str | None, pid: int | None) -> int:
        row = self._one(
            """INSERT INTO bot_runs ([trigger], status, host, pid) OUTPUT inserted.id
               VALUES (:t, N'running', :h, :p)""",
            {"t": trigger, "h": host, "p": pid},
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
            """UPDATE bot_runs SET status = :s, exit_code = :e, summary = :sum, error_message = :m,
                 finished_at = SYSUTCDATETIME() WHERE id = :id""",
            {
                "s": status,
                "e": exit_code,
                "sum": json.dumps(summary, ensure_ascii=False, default=str) if summary else None,
                "m": _clip(error_message, 4000),
                "id": run_id,
            },
        )

    def close_abandoned_bot_runs(self) -> int:
        """Ejecuciones que quedaron "running" porque el proceso murió sin cerrarlas."""
        return self._exec(
            """UPDATE bot_runs SET status = N'failed',
                 error_message = N'La ejecución terminó de forma inesperada',
                 finished_at = SYSUTCDATETIME() WHERE status = N'running'"""
        )

    def list_bot_runs(self, limit: int = 10) -> list[Row]:
        rows = self._all("SELECT TOP (:n) * FROM bot_runs ORDER BY id DESC", {"n": int(limit)})
        for row in rows:
            row["summary"] = json.loads(row["summary"]) if row.get("summary") else None
        return rows

    def get_bot_run(self, run_id: int) -> Row | None:
        row = self._one("SELECT * FROM bot_runs WHERE id = :id", {"id": run_id})
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
        where = [
            "supplier_ruc IS NOT NULL",
            "series IS NOT NULL",
            "number IS NOT NULL",
            "issue_date IS NOT NULL",
            "document_type IN :types",
        ]
        params: dict[str, Any] = {"types": list(document_types)}
        if month:
            params["from"], params["to"] = month_range(month)
            where.append("issue_date >= :from AND issue_date < :to")
        if not force:
            where.append("(sunat_checked_at IS NULL OR sunat_status = N'NO EXISTE')")
        top = f"TOP ({int(limit)}) " if limit else ""
        return self._all(
            f"SELECT {top}* FROM invoices WHERE {' AND '.join(where)} ORDER BY issue_date, id",
            params,
            expanding=("types",),
        )

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
                """INSERT INTO sunat_checks (invoice_id, status, estado_cp, estado_ruc,
                     cond_domi_ruc, observations, error_kind, error_message, raw_response)
                   VALUES (:i, :s, :cp, :ruc, :dom, :obs, :ek, :em, :raw)""",
                {
                    "i": invoice_id,
                    "s": "failed" if error else "success",
                    "cp": result.estado_cp if result else None,
                    "ruc": result.estado_ruc if result else None,
                    "dom": result.cond_domi_ruc if result else None,
                    "obs": json.dumps(result.observaciones, ensure_ascii=False) if result else None,
                    "ek": error[0] if error else None,
                    "em": _clip(error[1], 4000) if error else None,
                    "raw": json.dumps(result.raw, ensure_ascii=False, default=str)
                    if result
                    else None,
                },
            )
            if error:
                return None
            repo._exec(
                "DELETE FROM validation_issues WHERE invoice_id = :i AND LEFT(rule_code, :n) = :p",
                {"i": invoice_id, "n": len(SUNAT_RULE_PREFIX), "p": SUNAT_RULE_PREFIX},
            )
            repo.insert_issues(invoice_id, issues)
            open_rows = repo._all(
                "SELECT severity FROM validation_issues WHERE invoice_id = :i AND resolved_at IS NULL",
                {"i": invoice_id},
            )
            status = status_from_issues(open_rows)
            repo._exec(
                """UPDATE invoices SET sunat_status = :rs, sunat_checked_at = SYSUTCDATETIME(),
                     validation_status = :vs, updated_at = SYSUTCDATETIME() WHERE id = :i""",
                {"rs": receipt_status, "vs": status, "i": invoice_id},
            )
            return status
