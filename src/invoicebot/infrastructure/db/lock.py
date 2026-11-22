"""Candado de ejecución en SQL Server (sp_getapplock, dueño la sesión).

Impide dos ciclos del bot a la vez aunque corran en máquinas o contenedores
distintos (p. ej. una ejecución programada del Container Apps Job y otra
lanzada desde la API). Si el proceso muere, SQL Server libera el candado al
cerrarse su conexión.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import text

from invoicebot.errors import LockedError
from invoicebot.infrastructure.db.connection import Database

LOCK_NAME = "invoicebot:run"


def _holder(db: Database) -> str:
    with db.connect() as conn:
        row = conn.execute(
            text(
                "SELECT TOP (1) host, pid, started_at FROM bot_runs "
                "WHERE status = N'running' ORDER BY id DESC"
            )
        ).first()
    if row is None:
        return ""
    return f" (PID {row.pid} en {row.host}, desde {row.started_at:%Y-%m-%d %H:%M} UTC)"


@contextmanager
def run_lock(db: Database, name: str = LOCK_NAME) -> Iterator[None]:
    """Reserva el candado mientras dura el bloque; lanza LockedError si está tomado."""
    conn: Any = db.raw_connection(autocommit=True)
    try:
        cursor = conn.cursor()
        result = cursor.execute(
            "SET NOCOUNT ON; DECLARE @r INT; "
            "EXEC @r = sp_getapplock @Resource = ?, @LockMode = 'Exclusive', "
            "@LockOwner = 'Session', @LockTimeout = 0; SELECT @r",
            name,
        ).fetchone()[0]
        if result < 0:
            raise LockedError(f"Ya hay una ejecución en curso{_holder(db)}")
        try:
            yield
        finally:
            cursor.execute("EXEC sp_releaseapplock @Resource = ?, @LockOwner = 'Session'", name)
    finally:
        conn.close()
