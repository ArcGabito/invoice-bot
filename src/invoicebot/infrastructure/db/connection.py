"""Conexión con SQL Server (pyodbc + SQLAlchemy Core).

Admite tres formas de autenticación:

- ``sql``: usuario y contraseña de SQL Server (INVOICEBOT_DB_USER / _PASSWORD).
- ``windows``: seguridad integrada (Trusted_Connection), p. ej. ``TUF-F16\\GABODB``
  con la cuenta de Windows que ejecuta el bot o una cuenta de servicio (gMSA).
- ``managed-identity``: identidad administrada de Azure (Container Apps, VM) contra
  Azure SQL Database, sin contraseñas.

INVOICEBOT_DB_CONNECTION_STRING acepta una cadena ODBC completa o la de SSMS /
ADO.NET (``Data Source=…;Integrated Security=True;…``), que se traduce a ODBC.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

import pyodbc
from sqlalchemy import Connection, Engine, create_engine, event, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from invoicebot.config import DbConfig
from invoicebot.errors import ConfigError, InvoiceBotError

# SQLAlchemy gestiona el pool: el de pyodbc/ODBC se desactiva.
pyodbc.pooling = False

#: SERVERPROPERTY('EngineEdition'): 5 = Azure SQL Database, 8 = Azure SQL Managed Instance.
AZURE_SQL_DATABASE = 5
AZURE_SQL_MANAGED_INSTANCE = 8

_ADO_AUTH = {
    "active directory managed identity": "ActiveDirectoryMsi",
    "active directory msi": "ActiveDirectoryMsi",
    "active directory password": "ActiveDirectoryPassword",
    "active directory integrated": "ActiveDirectoryIntegrated",
    "active directory interactive": "ActiveDirectoryInteractive",
    "active directory service principal": "ActiveDirectoryServicePrincipal",
    "sql password": "SqlPassword",
}


def parse_connection_string(value: str) -> dict[str, str]:
    """Separa "clave=valor;…" respetando comillas y llaves. Claves en minúsculas."""
    result: dict[str, str] = {}
    i, n = 0, len(value)
    while i < n:
        while i < n and value[i] in "; \t\r\n":
            i += 1
        if i >= n:
            break
        eq = value.find("=", i)
        if eq == -1:
            raise ConfigError(f"Cadena de conexión no válida cerca de: {value[i : i + 30]!r}")
        key = value[i:eq].strip().lower()
        i = eq + 1
        while i < n and value[i] == " ":
            i += 1
        if i < n and value[i] in "\"'{":
            closing = "}" if value[i] == "{" else value[i]
            i += 1
            chars: list[str] = []
            while i < n:
                if value[i] == closing:
                    if i + 1 < n and value[i + 1] == closing:
                        chars.append(closing)
                        i += 2
                        continue
                    i += 1
                    break
                chars.append(value[i])
                i += 1
            parsed = "".join(chars)
        else:
            end = value.find(";", i)
            end = n if end == -1 else end
            parsed = value[i:end].strip()
            i = end
        result[key] = parsed
    return result


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("true", "yes", "sspi", "1", "mandatory", "strict")


def _odbc_value(value: str) -> str:
    if re.search(r"[;{}=\s]", value) or value != value.strip():
        return "{" + value.replace("}", "}}") + "}"
    return value


@dataclass(frozen=True)
class OdbcTarget:
    """Cadena ODBC ya construida y datos para describirla sin secretos."""

    connection_string: str
    server: str
    database: str
    auth: str
    login_timeout: int

    def describe(self) -> str:
        return f"{self.server}/{self.database} ({self.auth})"


def build_target(
    db: DbConfig,
    env: Mapping[str, str],
    *,
    database: str | None = None,
    admin: bool = False,
    app_name: str = "InvoiceBot",
) -> OdbcTarget:
    """Construye la cadena ODBC para la base de la aplicación (o `database`)."""
    parts: dict[str, str]
    auth = db.auth
    login_timeout = 15
    if db.connection_string:
        raw = parse_connection_string(db.connection_string)
        if "driver" in raw:
            parts = {k: v for k, v in raw.items() if k != "driver"}
            driver = raw["driver"]
            if parts.get("trusted_connection", "").lower() == "yes":
                auth = "windows"
            elif parts.get("authentication", "").lower() == "activedirectorymsi":
                auth = "managed-identity"
            elif "uid" in parts:
                auth = "sql"
        else:
            driver = db.driver
            parts = _from_ado(raw)
            auth = (
                "windows"
                if parts.get("trusted_connection") == "yes"
                else "managed-identity"
                if parts.get("authentication") == "ActiveDirectoryMsi"
                else "sql"
            )
            if raw.get("connect timeout") or raw.get("connection timeout"):
                with suppress(ValueError):
                    login_timeout = int(raw.get("connect timeout") or raw["connection timeout"])
        parts.setdefault("database", db.name)
    else:
        driver = db.driver
        server = db.server if db.port is None else f"{db.server},{db.port}"
        parts = {
            "server": server,
            "database": db.name,
            "encrypt": "yes" if db.encrypt else "no",
            "trustservercertificate": "yes" if db.trust_server_certificate else "no",
        }
        if auth == "windows":
            parts["trusted_connection"] = "yes"
        elif auth == "managed-identity":
            parts["authentication"] = "ActiveDirectoryMsi"
            if db.managed_identity_client_id:
                parts["uid"] = db.managed_identity_client_id
        else:
            user = env.get("INVOICEBOT_DB_ADMIN_USER") if admin else db.user
            password = (
                env.get("INVOICEBOT_DB_ADMIN_PASSWORD")
                if admin
                else env.get("INVOICEBOT_DB_PASSWORD")
            )
            if not user or not password:
                which = (
                    "INVOICEBOT_DB_ADMIN_USER/_PASSWORD"
                    if admin
                    else "INVOICEBOT_DB_USER/_PASSWORD"
                )
                raise ConfigError(
                    f"Falta el usuario o la contraseña de SQL Server ({which}), o usa "
                    "INVOICEBOT_DB_AUTH=windows para la seguridad integrada de Windows"
                )
            parts["uid"] = user
            parts["pwd"] = password
    if admin and env.get("INVOICEBOT_DB_ADMIN_USER") and env.get("INVOICEBOT_DB_ADMIN_PASSWORD"):
        # `db init` con una cuenta de administrador de SQL Server explícita.
        for key in ("trusted_connection", "authentication"):
            parts.pop(key, None)
        parts["uid"] = env["INVOICEBOT_DB_ADMIN_USER"]
        parts["pwd"] = env["INVOICEBOT_DB_ADMIN_PASSWORD"]
        auth = "sql"
    if database is not None:
        parts["database"] = database
    parts["app"] = app_name
    connection = f"DRIVER={{{driver}}};" + ";".join(
        f"{key.upper()}={_odbc_value(value)}" for key, value in parts.items()
    )
    return OdbcTarget(connection, parts.get("server", "?"), parts["database"], auth, login_timeout)


def _from_ado(raw: Mapping[str, str]) -> dict[str, str]:
    """Traduce una cadena de SSMS / ADO.NET (Microsoft.Data.SqlClient) a claves ODBC."""
    parts: dict[str, str] = {}
    for key in ("data source", "server", "address", "addr", "network address"):
        if raw.get(key):
            parts["server"] = raw[key]
            break
    if "server" not in parts:
        raise ConfigError("La cadena de conexión no indica el servidor (Data Source)")
    for key in ("initial catalog", "database"):
        if raw.get(key):
            parts["database"] = raw[key]
            break
    if _truthy(raw.get("integrated security") or raw.get("trusted_connection")):
        parts["trusted_connection"] = "yes"
    authentication = (raw.get("authentication") or "").strip().lower()
    if authentication and authentication in _ADO_AUTH:
        parts["authentication"] = _ADO_AUTH[authentication]
    user = raw.get("user id") or raw.get("uid") or raw.get("user")
    if user:
        parts["uid"] = user
    password = raw.get("password") or raw.get("pwd")
    if password:
        parts["pwd"] = password
    encrypt = raw.get("encrypt")
    parts["encrypt"] = "no" if encrypt and encrypt.lower() in ("false", "no", "optional") else "yes"
    parts["trustservercertificate"] = "yes" if _truthy(raw.get("trustservercertificate")) else "no"
    if _truthy(raw.get("multipleactiveresultsets")):
        parts["mars_connection"] = "yes"
    if raw.get("applicationintent"):
        parts["applicationintent"] = raw["applicationintent"]
    if raw.get("multisubnetfailover"):
        parts["multisubnetfailover"] = "yes" if _truthy(raw["multisubnetfailover"]) else "no"
    return parts


#: Empresas que puede ver quien hace la petición (None = todas). Lo fija la API por
#: petición; los workers, la CLI y el programador no lo tocan y ven todo.
COMPANY_SCOPE: ContextVar[tuple[int, ...] | None] = ContextVar(
    "invoicebot_company_scope", default=None
)
#: Quién hace el cambio, para la auditoría (trigger de `invoices`): una persona, una clave
#: de API ("api:erp") o un trabajo ("job:documents.process#12").
ACTOR: ContextVar[str | None] = ContextVar("invoicebot_actor", default=None)
_SCOPE_KEY = "invoicebot.companies"
_ACTOR_KEY = "invoicebot.actor"
#: Valor que no coincide con ninguna empresa: alguien limitado a ninguna no ve nada.
_NO_COMPANY = "0"


def company_scope_value(scope: tuple[int, ...] | None) -> str | None:
    if scope is None:
        return None
    return ",".join(str(int(c)) for c in scope) or _NO_COMPANY


def apply_company_scope(dbapi_connection: Any, info: dict[str, Any]) -> None:
    """Alinea SESSION_CONTEXT con la petición o el trabajo actual.

    - ``invoicebot.companies``: la Row-Level Security (migración 007) filtra `invoices`.
    - ``invoicebot.actor``: el trigger de auditoría registra quién hizo el cambio.

    Las conexiones del pool se reutilizan: solo se envía lo que cambió.
    """
    wanted = {
        _SCOPE_KEY: company_scope_value(COMPANY_SCOPE.get()),
        _ACTOR_KEY: (ACTOR.get() or None),
    }
    changed = [(k, v) for k, v in wanted.items() if info.get(k) != v]
    if not changed:
        return
    cursor = dbapi_connection.cursor()
    try:
        for key, value in changed:
            cursor.execute(
                "EXEC sp_set_session_context @key = ?, @value = ?",
                key,
                value[:150] if value and key == _ACTOR_KEY else value,
            )
            info[key] = value
    finally:
        cursor.close()


class Database:
    """Motor de SQLAlchemy sobre pyodbc para una base de datos concreta."""

    def __init__(
        self,
        target: OdbcTarget,
        *,
        pool_size: int = 10,
        pooled: bool = True,
    ):
        self.target = target

        def creator() -> Any:
            try:
                return pyodbc.connect(target.connection_string, timeout=target.login_timeout)
            except pyodbc.Error as error:
                raise connection_error(error, target) from error

        if pooled:
            self.engine: Engine = create_engine(
                "mssql+pyodbc://",
                creator=creator,
                pool_size=pool_size,
                max_overflow=pool_size,
                pool_pre_ping=True,
                pool_recycle=1800,
            )
        else:
            self.engine = create_engine("mssql+pyodbc://", creator=creator, poolclass=NullPool)

        @event.listens_for(self.engine, "connect")
        def _on_connect(dbapi_connection: Any, _record: Any) -> None:
            # Lectura de NVARCHAR(MAX) y DATETIME2 sin sorpresas de codificación.
            dbapi_connection.setdecoding(pyodbc.SQL_WCHAR, encoding="utf-16le")

        @event.listens_for(self.engine, "checkout")
        def _on_checkout(dbapi_connection: Any, record: Any, _proxy: Any) -> None:
            apply_company_scope(dbapi_connection, record.info)

        self._edition: int | None = None
        self._lock = threading.Lock()

    @classmethod
    def from_config(
        cls,
        db: DbConfig,
        env: Mapping[str, str],
        *,
        database: str | None = None,
        admin: bool = False,
        pooled: bool = True,
        app_name: str = "InvoiceBot",
    ) -> Database:
        target = build_target(db, env, database=database, admin=admin, app_name=app_name)
        return cls(target, pool_size=db.pool_size, pooled=pooled)

    @property
    def name(self) -> str:
        return self.target.database

    def describe(self) -> str:
        return self.target.describe()

    @contextmanager
    def begin(self) -> Iterator[Connection]:
        with self.engine.begin() as conn:
            yield conn

    @contextmanager
    def connect(self) -> Iterator[Connection]:
        with self.engine.connect() as conn:
            yield conn

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Sesión ORM en una transacción: se confirma al salir sin error.

        Usa el mismo motor (y el mismo pool) que el resto: aplica igual la Row-Level
        Security por empresa y el actor de la auditoría.
        """
        with Session(self.engine, expire_on_commit=False) as session, session.begin():
            yield session

    def raw_connection(self, *, autocommit: bool = True) -> Any:
        """Conexión pyodbc fuera del pool (candados de sesión, BACKUP DATABASE)."""
        try:
            conn = pyodbc.connect(
                self.target.connection_string,
                timeout=self.target.login_timeout,
                autocommit=autocommit,
            )
        except pyodbc.Error as error:
            raise connection_error(error, self.target) from error
        return conn

    def engine_edition(self) -> int:
        with self._lock:
            if self._edition is None:
                with self.connect() as conn:
                    self._edition = int(
                        conn.execute(
                            text("SELECT CAST(SERVERPROPERTY('EngineEdition') AS INT)")
                        ).scalar_one()
                    )
            return self._edition

    def is_azure_sql(self) -> bool:
        return self.engine_edition() in (AZURE_SQL_DATABASE, AZURE_SQL_MANAGED_INSTANCE)

    def ping(self) -> str:
        with self.connect() as conn:
            row = conn.execute(
                text("SELECT @@VERSION AS version, DB_NAME() AS db, SUSER_NAME() AS login")
            ).one()
        first_line = str(row.version).splitlines()[0].strip()
        return f"{first_line} · base {row.db} · inicio de sesión {row.login}"

    def dispose(self) -> None:
        self.engine.dispose()


class DatabaseConnectionError(InvoiceBotError):
    code = "DB_CONNECTION_ERROR"


def connection_error(error: BaseException, target: OdbcTarget) -> DatabaseConnectionError:
    """Error de conexión legible, con pistas según el código ODBC."""
    detail = str(error)
    hint = ""
    lowered = detail.lower()
    if "im002" in lowered or "data source name not found" in lowered:
        hint = (
            " Falta el controlador ODBC: instala «ODBC Driver 18 for SQL Server» "
            "(o ajusta INVOICEBOT_DB_DRIVER)."
        )
    elif "18456" in detail or "login failed" in lowered:
        hint = " SQL Server rechazó el inicio de sesión: revisa usuario, contraseña o permisos."
    elif "4060" in detail or "cannot open database" in lowered:
        hint = " La base de datos no existe o no tienes acceso: ejecuta `invoicebot db init`."
    elif "certificate" in lowered or "ssl" in lowered:
        hint = (
            " El certificado del servidor no es de confianza: usa un certificado válido o, solo "
            "en una red de confianza, INVOICEBOT_DB_TRUST_CERT=true."
        )
    elif "08001" in detail or "timeout" in lowered or "network" in lowered:
        hint = " No se alcanza el servidor: revisa el nombre, el puerto, TCP/IP y el firewall."
    return DatabaseConnectionError(
        f"No se pudo conectar con SQL Server {target.describe()}: {detail}.{hint}"
    )
