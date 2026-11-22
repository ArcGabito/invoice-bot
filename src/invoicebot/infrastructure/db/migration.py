"""Ejecutor de migraciones del esquema.

Cada migración es una lista de lotes T-SQL (un driver no admite ``GO`` y algunas
sentencias, como CREATE VIEW, deben ir solas). Los lotes de una migración se
aplican en una transacción. Antes de migrar una base que ya tenía datos se hace
una copia de seguridad (salvo en Azure SQL, que tiene copias automáticas).

Convenciones:
- Importes en céntimos (BIGINT, columnas *_cents); la vista v_invoices los
  ofrece en DECIMAL(18,2) para informes (Power BI, Excel).
- Texto en NVARCHAR: los parámetros de pyodbc son NVARCHAR y compararlos con
  columnas VARCHAR obligaría a convertir la columna y perder los índices.
- Fechas de auditoría en DATETIME2(3) UTC; fechas de comprobante en DATE.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text

from invoicebot.infrastructure.db.connection import Database


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    batches: tuple[str, ...]


@dataclass
class MigrationResult:
    applied: list[str] = field(default_factory=list)
    backup: str | None = None


_CREATE_TABLE = """
IF OBJECT_ID(N'schema_migrations', N'U') IS NULL
  CREATE TABLE schema_migrations (
    version    INT           NOT NULL CONSTRAINT pk_schema_migrations PRIMARY KEY,
    name       NVARCHAR(200) NOT NULL,
    applied_at DATETIME2(3)  NOT NULL CONSTRAINT df_schema_migrations_applied DEFAULT SYSUTCDATETIME()
  )
"""


def all_migrations() -> tuple[Migration, ...]:
    from invoicebot.infrastructure.db.migrations_v0 import V0_MIGRATIONS
    from invoicebot.infrastructure.db.migrations_v1 import V1_MIGRATIONS

    return (*V0_MIGRATIONS, *V1_MIGRATIONS)


def applied_versions(db: Database) -> set[int]:
    with db.connect() as conn:
        exists = conn.execute(text("SELECT OBJECT_ID(N'schema_migrations', N'U')")).scalar()
        if not exists:
            return set()
        return {
            int(v) for v in conn.execute(text("SELECT version FROM schema_migrations")).scalars()
        }


def schema_status(db: Database) -> dict[str, Any]:
    migrations = all_migrations()
    applied = applied_versions(db)
    latest = max(m.version for m in migrations)
    current = max(applied) if applied else 0
    return {
        "current": current,
        "latest": latest,
        "pending": [m.name for m in migrations if m.version not in applied],
        "ahead": current > latest,
    }


def run_migrations(
    db: Database,
    *,
    backup: Callable[[str], str | None] | None = None,
    up_to: int | None = None,
    migrations: Sequence[Migration] | None = None,
) -> MigrationResult:
    """Aplica las migraciones pendientes, cada una en su transacción."""
    migrations = tuple(migrations or all_migrations())
    with db.begin() as conn:
        conn.exec_driver_sql(_CREATE_TABLE)
    applied = applied_versions(db)
    pending = [
        m for m in migrations if m.version not in applied and (up_to is None or m.version <= up_to)
    ]
    result = MigrationResult()
    if not pending:
        return result
    if backup is not None and applied:
        result.backup = backup(f"v{max(applied)}")
    for migration in pending:
        with db.begin() as conn:
            for batch in migration.batches:
                conn.exec_driver_sql(batch)
            conn.execute(
                text("INSERT INTO schema_migrations (version, name) VALUES (:v, :n)"),
                {"v": migration.version, "n": migration.name},
            )
        result.applied.append(migration.name)
    return result
