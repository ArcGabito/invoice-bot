"""Copias de seguridad verificadas con BACKUP DATABASE … WITH COPY_ONLY, CHECKSUM.

COPY_ONLY no altera la cadena de copias que TI tenga programada. El archivo lo
escribe el servicio de SQL Server: INVOICEBOT_BACKUP_DIR es una ruta DEL
SERVIDOR. En Azure SQL Database no hay copias a disco (las hace Azure).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import text

from invoicebot.errors import InvoiceBotError
from invoicebot.infrastructure.db.connection import Database

_BACKUP_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*-(?:[A-Za-z0-9]+-)?(\d{8})-(\d{6})\.bak$")


class BackupUnavailableError(InvoiceBotError):
    code = "BACKUP_UNAVAILABLE"


@dataclass
class BackupInfo:
    name: str
    path: str
    created_at: datetime
    size: int | None


@dataclass
class BackupResult:
    path: str
    size: int | None
    removed: list[str] = field(default_factory=list)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y%m%d-%H%M%S")


def _server_path(directory: str, name: str) -> str:
    sep = "\\" if "\\" in directory and "/" not in directory else "/"
    return directory.rstrip("\\/") + sep + name


def _require_disk_backups(db: Database) -> None:
    if db.is_azure_sql():
        raise BackupUnavailableError(
            "Azure SQL hace copias automáticas (restauración a un punto en el tiempo): "
            "BACKUP DATABASE a disco no está disponible"
        )


def list_backups(db: Database, limit: int = 50) -> list[BackupInfo]:
    if db.is_azure_sql():
        return []
    with db.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT TOP (:limit) f.physical_device_name AS path,
                  DATEADD(MINUTE, DATEDIFF(MINUTE, SYSDATETIME(), SYSUTCDATETIME()),
                          s.backup_finish_date) AS finished,
                  s.backup_size AS size
                FROM msdb.dbo.backupset s
                JOIN msdb.dbo.backupmediafamily f ON f.media_set_id = s.media_set_id
                WHERE s.database_name = DB_NAME() AND s.type = 'D'
                  AND ABS(DATEDIFF(SECOND, s.database_creation_date,
                      (SELECT create_date FROM sys.databases WHERE database_id = DB_ID()))) <= 1
                ORDER BY s.backup_finish_date DESC
                """
            ),
            {"limit": limit},
        ).all()
    seen: set[str] = set()
    backups: list[BackupInfo] = []
    for row in rows:
        if row.path in seen:
            continue  # Dos copias en el mismo segundo comparten archivo.
        seen.add(row.path)
        name = re.split(r"[\\/]", row.path)[-1]
        if not _BACKUP_NAME.match(name):
            continue
        finished = row.finished.replace(tzinfo=UTC)
        backups.append(
            BackupInfo(name, row.path, finished, None if row.size is None else int(row.size))
        )
    return backups


def is_backup_due(db: Database, interval_hours: int, now: datetime | None = None) -> bool:
    if db.is_azure_sql():
        return False
    latest = list_backups(db, 1)
    now = now or datetime.now(UTC)
    return not latest or (now - latest[0].created_at).total_seconds() >= interval_hours * 3600


def default_backup_dir(db: Database) -> str:
    with db.connect() as conn:
        value = conn.execute(
            text("SELECT CAST(SERVERPROPERTY('InstanceDefaultBackupPath') AS NVARCHAR(4000))")
        ).scalar()
    if not value:
        raise BackupUnavailableError(
            "SQL Server no informa una carpeta de copias por defecto: define INVOICEBOT_BACKUP_DIR"
        )
    return str(value)


def create_backup(
    db: Database,
    directory: str | None,
    *,
    retention: int | None = None,
    label: str | None = None,
    now: datetime | None = None,
) -> BackupResult:
    """Copia verificada con RESTORE VERIFYONLY. Las de migración (`label`) no se borran."""
    _require_disk_backups(db)
    now = now or datetime.now(UTC)
    with db.connect() as conn:
        base = conn.execute(text("SELECT DB_NAME()")).scalar_one()
    name = f"{base}-{label}-{_stamp(now)}.bak" if label else f"{base}-{_stamp(now)}.bak"
    target = _server_path(directory or default_backup_dir(db), name)
    raw = db.raw_connection(autocommit=True)
    try:
        cursor = raw.cursor()
        cursor.execute(
            "DECLARE @db sysname = DB_NAME(); "
            "BACKUP DATABASE @db TO DISK = ? WITH COPY_ONLY, CHECKSUM, INIT, NAME = N'InvoiceBot'",
            target,
        )
        while cursor.nextset():  # BACKUP informa su progreso como mensajes.
            pass
        cursor.execute("RESTORE VERIFYONLY FROM DISK = ? WITH CHECKSUM", target)
        while cursor.nextset():
            pass
    finally:
        raw.close()

    removed: list[str] = []
    if retention is not None and not label:
        for old in list_backups(db, 500)[retention:]:
            if os.path.exists(old.path):
                os.remove(old.path)
                removed.append(old.name)
    size = os.path.getsize(target) if os.path.exists(target) else None
    return BackupResult(target, size, removed)
