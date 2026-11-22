"""Creación de la base de datos y de su inicio de sesión (`invoicebot db init`)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import text

from invoicebot.config import DbConfig
from invoicebot.errors import ConfigError
from invoicebot.infrastructure.db.connection import (
    AZURE_SQL_DATABASE,
    Database,
    connection_error,
)

_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
#: DOMINIO\cuenta o DOMINIO\gmsa$ (cuentas de servicio administradas).
_WINDOWS_LOGIN = re.compile(r"^[A-Za-z0-9._-]{1,64}\\[A-Za-z0-9._-]{1,64}\$?$")


def check_identifier(value: str, what: str) -> str:
    if not _IDENTIFIER.match(value):
        raise ConfigError(
            f'{what} "{value}" no es válido: usa letras, números y guion bajo, empezando por una '
            "letra"
        )
    return value


def _quote(value: str) -> str:
    return "N'" + value.replace("'", "''") + "'"


@dataclass
class InitResult:
    database_created: bool = False
    login_created: bool = False
    user_created: bool = False
    windows_login: str | None = None
    azure: bool = False


def init_database(db: DbConfig, env: Mapping[str, str]) -> InitResult:
    """Crea la base, activa READ_COMMITTED_SNAPSHOT y da acceso a la aplicación.

    Se conecta con la cuenta de administrador (INVOICEBOT_DB_ADMIN_USER/_PASSWORD)
    o, sin ella, con la misma autenticación de la aplicación (seguridad integrada
    de Windows o identidad administrada con permisos de administrador).
    """
    database = check_identifier(db.name, "El nombre de la base de datos")
    result = InitResult()
    master = Database.from_config(db, env, database="master", admin=True, pooled=False)
    try:
        result.azure = master.engine_edition() == AZURE_SQL_DATABASE
        with master.connect() as raw:
            conn = raw.execution_options(isolation_level="AUTOCOMMIT")
            exists = conn.execute(text("SELECT DB_ID(:name)"), {"name": database}).scalar()
            if not exists:
                conn.exec_driver_sql(f"CREATE DATABASE [{database}]")
                result.database_created = True
            if not result.azure:
                # Lecturas sin bloquear escrituras: el bot y la API trabajan a la vez.
                conn.exec_driver_sql(
                    f"ALTER DATABASE [{database}] SET READ_COMMITTED_SNAPSHOT ON "
                    "WITH ROLLBACK IMMEDIATE"
                )
            if db.auth == "sql" and not result.azure:
                if not db.user:
                    raise ConfigError(
                        "Falta INVOICEBOT_DB_USER (el inicio de sesión de la aplicación)"
                    )
                check_identifier(db.user, "El inicio de sesión")
                password = env.get("INVOICEBOT_DB_PASSWORD")
                if not password:
                    raise ConfigError("Falta INVOICEBOT_DB_PASSWORD para crear el inicio de sesión")
                if not conn.execute(text("SELECT SUSER_ID(:n)"), {"n": db.user}).scalar():
                    conn.exec_driver_sql(
                        f"CREATE LOGIN [{db.user}] WITH PASSWORD = {_quote(password)}, "
                        f"CHECK_POLICY = ON, DEFAULT_DATABASE = [{database}]"
                    )
                    result.login_created = True
            if db.windows_login:
                if not _WINDOWS_LOGIN.match(db.windows_login):
                    raise ConfigError(
                        "INVOICEBOT_DB_WINDOWS_LOGIN debe tener la forma DOMINIO\\cuenta "
                        "(o DOMINIO\\gmsa$)"
                    )
                if not conn.execute(text("SELECT SUSER_ID(:n)"), {"n": db.windows_login}).scalar():
                    conn.exec_driver_sql(
                        f"CREATE LOGIN [{db.windows_login}] FROM WINDOWS "
                        f"WITH DEFAULT_DATABASE = [{database}]"
                    )
                    result.login_created = True
                result.windows_login = db.windows_login
    except ConfigError:
        raise
    except Exception as error:
        raise connection_error(error, master.target) from error
    finally:
        master.dispose()

    grantee = db.user if db.auth == "sql" and not result.azure else db.windows_login
    if grantee:
        target = Database.from_config(db, env, database=database, admin=True, pooled=False)
        try:
            with target.connect() as raw:
                conn = raw.execution_options(isolation_level="AUTOCOMMIT")
                if not conn.execute(text("SELECT USER_ID(:n)"), {"n": grantee}).scalar():
                    conn.exec_driver_sql(f"CREATE USER [{grantee}] FOR LOGIN [{grantee}]")
                    result.user_created = True
                conn.exec_driver_sql(f"ALTER ROLE db_owner ADD MEMBER [{grantee}]")
        finally:
            target.dispose()
    return result


def create_empty_database(db: Database, name: str, *, replace: bool = False) -> None:
    """Crea una base vacía (pruebas). `db` es una conexión a master."""
    check_identifier(name, "El nombre de la base de datos")
    with db.connect() as raw:
        conn = raw.execution_options(isolation_level="AUTOCOMMIT")
        if replace:
            _drop(conn, name)
        conn.exec_driver_sql(f"CREATE DATABASE [{name}]")
        if db.engine_edition() != AZURE_SQL_DATABASE:
            conn.exec_driver_sql(
                f"ALTER DATABASE [{name}] SET READ_COMMITTED_SNAPSHOT ON WITH ROLLBACK IMMEDIATE"
            )


def drop_database(db: Database, name: str) -> None:
    check_identifier(name, "El nombre de la base de datos")
    with db.connect() as conn:
        _drop(conn.execution_options(isolation_level="AUTOCOMMIT"), name)


def _drop(conn: object, name: str) -> None:
    conn.exec_driver_sql(  # type: ignore[attr-defined]
        f"IF DB_ID(N'{name}') IS NOT NULL BEGIN "
        f"ALTER DATABASE [{name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; "
        f"DROP DATABASE [{name}]; END"
    )
