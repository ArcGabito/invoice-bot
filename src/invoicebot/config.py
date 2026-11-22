"""Configuración de InvoiceBot desde variables de entorno (y un archivo .env).

Los nombres de las variables son los mismos que en la versión 0.x (Node.js):
un .env existente sigue sirviendo. Los secretos (contraseñas, claves de API)
no se guardan en la configuración: solo si están definidos; se leen del
entorno en el momento de usarlos.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import dotenv_values

from invoicebot.errors import ConfigError

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
LOG_LEVELS = ("debug", "info", "warning", "error", "critical")
DEFAULT_ANTHROPIC_MODEL = "claude-opus-5-5"


@dataclass(frozen=True)
class AnthropicConfig:
    model: str
    effort: str
    api_key_set: bool


@dataclass(frozen=True)
class OllamaConfig:
    url: str
    model: str
    think: bool
    num_ctx: int
    timeout_s: float


@dataclass(frozen=True)
class SunatConfig:
    enabled: bool
    verify_on_scan: bool
    ruc: str | None
    client_id: str | None
    client_secret_set: bool
    timeout_s: float
    delay_s: float
    auth_url: str | None
    api_url: str | None


@dataclass(frozen=True)
class FxConfig:
    sunat: bool
    sunat_url: str | None
    timeout_s: float


@dataclass(frozen=True)
class ImapConfig:
    host: str | None
    port: int
    tls: bool


@dataclass(frozen=True)
class GoogleMailConfig:
    client_id: str | None
    client_secret_set: bool
    service_account_file: str | None


@dataclass(frozen=True)
class MicrosoftMailConfig:
    tenant: str
    client_id: str | None
    client_secret_set: bool
    cert_file: str | None


@dataclass(frozen=True)
class MailConfig:
    provider: str
    user: str | None
    password_set: bool
    dir: str | None
    folder: str | None
    processed_folder: str | None
    review_folder: str | None
    since_days: int
    max_messages: int
    timeout_s: float
    imap: ImapConfig
    google: GoogleMailConfig
    microsoft: MicrosoftMailConfig
    refresh_token_set: bool
    token_file: str


@dataclass(frozen=True)
class DbConfig:
    #: Cadena completa (ODBC o el formato de SSMS/ADO.NET). Tiene prioridad.
    connection_string: str | None
    driver: str
    server: str
    port: int | None
    name: str
    #: sql | windows | managed-identity
    auth: str
    user: str | None
    password_set: bool
    managed_identity_client_id: str | None
    encrypt: bool
    trust_server_certificate: bool
    pool_size: int
    #: Inicio de sesión de Windows (cuenta de servicio) al que `db init` da acceso.
    windows_login: str | None
    #: La API y la CLI aplican las migraciones pendientes al arrancar. Con varias réplicas
    #: (Container Apps, Compose con --scale) conviene false y un `invoicebot db migrate` previo.
    auto_migrate: bool = True


@dataclass(frozen=True)
class LogConfig:
    level: str
    dir: str | None
    retention_days: int
    format: str


@dataclass(frozen=True)
class BackupConfig:
    dir: str | None
    retention: int
    interval_hours: int


@dataclass(frozen=True)
class OcrConfig:
    engine: str
    languages: tuple[str, ...]
    max_pages: int
    scale: float
    min_confidence: int
    tesseract_cmd: str | None


@dataclass(frozen=True)
class ServeConfig:
    host: str
    port: int
    tls_cert: str | None
    tls_key: str | None
    session_hours: int
    #: auto (según el esquema de la petición) | true | false
    cookie_secure: str
    forwarded_allow_ips: str
    api_docs: bool
    upload_max_mb: int


@dataclass(frozen=True)
class RunConfig:
    inbox_dir: str | None
    recursive: bool
    export: str
    interval_minutes: int
    partial_exit_code: int


@dataclass(frozen=True)
class QueueConfig:
    #: inline: en el mismo proceso (Windows, pruebas) · redis: Dramatiq + Redis (VPS, Azure)
    backend: str
    #: La URL de Redis puede llevar contraseña: se lee del entorno al usarla.
    redis_url_set: bool
    namespace: str
    worker_threads: int
    #: Llamadas por minuto a la IA y a SUNAT entre todos los workers (0 = sin límite).
    ai_rate_per_minute: int
    sunat_rate_per_minute: int
    job_max_attempts: int


@dataclass(frozen=True)
class ScheduleConfig:
    """Expresiones cron (zona de la empresa). Vacío = la tarea no se programa."""

    cycle: str | None
    #: Correo + carpeta de entrada: registra los documentos nuevos y los encola.
    collect: str | None
    fx: str | None
    sunat: str | None
    retry: str | None
    export: str | None
    backup: str | None
    purge: str | None
    #: Días que se conservan los trabajos terminados.
    job_retention_days: int


@dataclass(frozen=True)
class Config:
    ai_provider: str
    anthropic: AnthropicConfig
    ollama: OllamaConfig
    sunat: SunatConfig
    fx: FxConfig
    mail: MailConfig
    data_dir: str
    db: DbConfig
    export_dir: str
    export_format: str
    upload_dir: str
    concurrency: int
    max_attempts: int
    min_text_chars: int
    amount_tolerance_cents: int
    log: LogConfig
    backup: BackupConfig
    ocr: OcrConfig
    serve: ServeConfig
    run: RunConfig
    queue: QueueConfig
    schedule: ScheduleConfig
    timezone: str
    reviewer: str | None
    env: Mapping[str, str] = field(repr=False, compare=False, default_factory=dict)

    def today(self) -> str:
        """Fecha local (zona de la empresa) AAAA-MM-DD."""
        return datetime.now(ZoneInfo(self.timezone)).date().isoformat()

    def secret(self, name: str) -> str | None:
        value = self.env.get(name)
        return value or None


class _Reader:
    """Lee y valida variables acumulando todos los errores en un solo mensaje."""

    def __init__(self, env: Mapping[str, str]):
        self.env = env
        self.errors: list[str] = []

    def text(self, name: str, default: str | None = None) -> str | None:
        value = self.env.get(name)
        if value is None or value.strip() == "":
            return default
        return value.strip()

    def is_set(self, name: str) -> bool:
        return bool((self.env.get(name) or "").strip())

    def integer(self, name: str, default: int, minimum: int, maximum: int) -> int:
        raw = self.text(name)
        if raw is None:
            return default
        try:
            value = int(raw)
        except ValueError:
            self.errors.append(f"{name}: debe ser un número entero (recibido: {raw!r})")
            return default
        if not minimum <= value <= maximum:
            self.errors.append(
                f"{name}: debe estar entre {minimum} y {maximum} (recibido: {value})"
            )
            return default
        return value

    def number(self, name: str, default: float, minimum: float, maximum: float) -> float:
        raw = self.text(name)
        if raw is None:
            return default
        try:
            value = float(raw)
        except ValueError:
            self.errors.append(f"{name}: debe ser un número (recibido: {raw!r})")
            return default
        if not minimum <= value <= maximum:
            self.errors.append(
                f"{name}: debe estar entre {minimum} y {maximum} (recibido: {value})"
            )
            return default
        return value

    def flag(self, name: str, default: bool) -> bool:
        raw = self.text(name)
        if raw is None:
            return default
        lowered = raw.lower()
        if lowered in ("true", "1", "yes", "si", "sí"):
            return True
        if lowered in ("false", "0", "no"):
            return False
        self.errors.append(f"{name}: usa true o false (recibido: {raw!r})")
        return default

    def choice(self, name: str, default: str, options: tuple[str, ...]) -> str:
        raw = (self.text(name) or default).lower()
        if raw not in options:
            self.errors.append(f"{name}: usa {' | '.join(options)} (recibido: {raw!r})")
            return default
        return raw

    def url(self, name: str) -> str | None:
        raw = self.text(name)
        if raw is not None and not re.match(r"^https?://", raw):
            self.errors.append(f"{name}: debe ser una URL http(s)")
            return None
        return raw


def load_env(cwd: Path | None = None, env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Entorno efectivo: el .env del directorio de trabajo, con prioridad del entorno real."""
    base = Path(cwd or Path.cwd())
    merged: dict[str, str] = {}
    env_file = Path(os.environ.get("INVOICEBOT_ENV_FILE", base / ".env"))
    if env_file.is_file():
        merged.update({k: v for k, v in dotenv_values(env_file).items() if v is not None})
    merged.update(os.environ if env is None else env)
    return merged


def _resolve(cwd: Path, value: str | None) -> str | None:
    if value is None:
        return None
    path = Path(value).expanduser()
    return str(path if path.is_absolute() else (cwd / path).resolve())


def load_config(
    env: Mapping[str, str] | None = None,
    *,
    cwd: Path | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> Config:
    """Construye y valida la configuración. Lanza ConfigError con todos los problemas."""
    cwd = Path(cwd or Path.cwd())
    env = load_env(cwd) if env is None else dict(env)
    overrides = dict(overrides or {})
    r = _Reader(env)

    data_dir = _resolve(cwd, overrides.get("data_dir") or r.text("INVOICEBOT_DATA_DIR", "./data"))
    assert data_dir is not None
    export_dir = _resolve(cwd, r.text("INVOICEBOT_EXPORT_DIR", "./exports"))
    assert export_dir is not None
    inbox_dir = _resolve(cwd, r.text("INVOICEBOT_INBOX_DIR"))

    tolerance_raw = r.text("INVOICEBOT_AMOUNT_TOLERANCE", "0.02") or "0.02"
    try:
        tolerance_cents = round(float(tolerance_raw) * 100)
        if not 0 <= tolerance_cents <= 10_000:
            raise ValueError
    except ValueError:
        r.errors.append("INVOICEBOT_AMOUNT_TOLERANCE: usa un importe como 0.02")
        tolerance_cents = 2

    db_user = r.text("INVOICEBOT_DB_USER")
    db_auth_default = "sql" if db_user else "windows"
    db_server = r.text("INVOICEBOT_DB_SERVER", "localhost") or "localhost"
    db_port_raw = r.text("INVOICEBOT_DB_PORT")
    db_port: int | None
    if db_port_raw is None:
        # Una instancia con nombre (SERVIDOR\INSTANCIA) usa el puerto que anuncia SQL Browser.
        db_port = None if "\\" in db_server else 1433
    else:
        db_port = r.integer("INVOICEBOT_DB_PORT", 1433, 1, 65535)

    ocr_languages = tuple(
        lang.strip()
        for lang in (r.text("INVOICEBOT_OCR_LANGUAGES", "spa,eng") or "").split(",")
        if lang.strip()
    )
    for lang in ocr_languages:
        if not re.fullmatch(r"[a-z_]{3,8}", lang):
            r.errors.append(
                f"INVOICEBOT_OCR_LANGUAGES: {lang!r} no es un código de Tesseract (p. ej. spa)"
            )

    mail_provider = r.choice("INVOICEBOT_MAIL_PROVIDER", "none", ("none", "imap", "gmail", "graph"))
    ms_secret = r.is_set("INVOICEBOT_MAIL_MS_CLIENT_SECRET")
    ms_cert = _resolve(cwd, r.text("INVOICEBOT_MAIL_MS_CERT_FILE"))
    if ms_secret and ms_cert:
        r.errors.append(
            "Usa INVOICEBOT_MAIL_MS_CLIENT_SECRET o INVOICEBOT_MAIL_MS_CERT_FILE, no ambos"
        )
    tenant = r.text("INVOICEBOT_MAIL_MS_TENANT", "common") or "common"
    if not re.fullmatch(r"[A-Za-z0-9.-]+", tenant):
        r.errors.append("INVOICEBOT_MAIL_MS_TENANT: ID de directorio o dominio")

    sunat_enabled = r.flag("INVOICEBOT_SUNAT_ENABLED", False)
    sunat_on_scan = r.flag("INVOICEBOT_SUNAT_VERIFY_ON_SCAN", False)
    if sunat_on_scan and not sunat_enabled:
        r.errors.append("INVOICEBOT_SUNAT_VERIFY_ON_SCAN requiere INVOICEBOT_SUNAT_ENABLED=true")

    tls_cert = _resolve(cwd, r.text("INVOICEBOT_SERVE_TLS_CERT"))
    tls_key = _resolve(cwd, r.text("INVOICEBOT_SERVE_TLS_KEY"))
    if bool(tls_cert) != bool(tls_key):
        r.errors.append("INVOICEBOT_SERVE_TLS_CERT e INVOICEBOT_SERVE_TLS_KEY van juntos")

    timezone = r.text("INVOICEBOT_TIMEZONE", r.text("TZ", "America/Lima")) or "America/Lima"
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        r.errors.append(f"INVOICEBOT_TIMEZONE: zona horaria desconocida {timezone!r}")
        timezone = "America/Lima"

    redis_url_set = r.is_set("INVOICEBOT_REDIS_URL")
    queue_backend = r.choice(
        "INVOICEBOT_QUEUE_BACKEND", "redis" if redis_url_set else "inline", ("inline", "redis")
    )
    if queue_backend == "redis" and not redis_url_set:
        r.errors.append("INVOICEBOT_QUEUE_BACKEND=redis requiere INVOICEBOT_REDIS_URL")
    namespace = r.text("INVOICEBOT_QUEUE_NAMESPACE", "invoicebot") or "invoicebot"
    if not re.fullmatch(r"[a-z0-9_-]{1,40}", namespace):
        r.errors.append("INVOICEBOT_QUEUE_NAMESPACE: minúsculas, dígitos, - y _")

    from invoicebot.domain.cron import CronError, parse_cron

    def cron(name: str, default: str | None) -> str | None:
        raw = r.text(name, default)
        if raw is None or raw.lower() in ("off", "none", "-"):
            return None
        try:
            parse_cron(raw)
        except CronError as error:
            r.errors.append(f"{name}: {error}")
            return None
        return raw

    run_interval = r.integer("INVOICEBOT_RUN_INTERVAL_MINUTES", 15, 1, 24 * 60)

    provider = str(
        overrides.get("ai_provider") or r.text("INVOICEBOT_AI_PROVIDER") or "anthropic"
    ).lower()
    if provider not in ("anthropic", "ollama", "mock"):
        r.errors.append(
            f"INVOICEBOT_AI_PROVIDER: usa anthropic | ollama | mock (recibido: {provider!r})"
        )
        provider = "mock"

    config = Config(
        ai_provider=provider,
        anthropic=AnthropicConfig(
            model=r.text("INVOICEBOT_ANTHROPIC_MODEL", DEFAULT_ANTHROPIC_MODEL)
            or DEFAULT_ANTHROPIC_MODEL,
            effort=r.choice("INVOICEBOT_ANTHROPIC_EFFORT", "medium", EFFORT_LEVELS),
            api_key_set=r.is_set("ANTHROPIC_API_KEY"),
        ),
        ollama=OllamaConfig(
            url=r.url("INVOICEBOT_OLLAMA_URL") or "http://127.0.0.1:11434",
            model=r.text("INVOICEBOT_OLLAMA_MODEL", "qwen3.5:4b") or "qwen3.5:4b",
            think=r.flag("INVOICEBOT_OLLAMA_THINK", False),
            num_ctx=r.integer("INVOICEBOT_OLLAMA_NUM_CTX", 8192, 2048, 262_144),
            timeout_s=r.integer("INVOICEBOT_OLLAMA_TIMEOUT_MS", 600_000, 10_000, 3_600_000) / 1000,
        ),
        sunat=SunatConfig(
            enabled=sunat_enabled,
            verify_on_scan=sunat_on_scan,
            ruc=r.text("INVOICEBOT_SUNAT_RUC"),
            client_id=r.text("INVOICEBOT_SUNAT_CLIENT_ID"),
            client_secret_set=r.is_set("INVOICEBOT_SUNAT_CLIENT_SECRET"),
            timeout_s=r.integer("INVOICEBOT_SUNAT_TIMEOUT_MS", 20_000, 1000, 120_000) / 1000,
            delay_s=r.integer("INVOICEBOT_SUNAT_DELAY_MS", 0, 0, 60_000) / 1000,
            auth_url=r.url("INVOICEBOT_SUNAT_AUTH_URL"),
            api_url=r.url("INVOICEBOT_SUNAT_API_URL"),
        ),
        fx=FxConfig(
            sunat=r.flag("INVOICEBOT_FX_SUNAT", True),
            sunat_url=r.url("INVOICEBOT_FX_SUNAT_URL"),
            timeout_s=r.integer("INVOICEBOT_FX_TIMEOUT_MS", 20_000, 1000, 120_000) / 1000,
        ),
        mail=MailConfig(
            provider=mail_provider,
            user=r.text("INVOICEBOT_MAIL_USER"),
            password_set=r.is_set("INVOICEBOT_MAIL_PASSWORD"),
            dir=_resolve(cwd, r.text("INVOICEBOT_MAIL_DIR")),
            folder=r.text("INVOICEBOT_MAIL_FOLDER"),
            processed_folder=r.text("INVOICEBOT_MAIL_PROCESSED_FOLDER"),
            review_folder=r.text("INVOICEBOT_MAIL_REVIEW_FOLDER"),
            since_days=r.integer("INVOICEBOT_MAIL_SINCE_DAYS", 7, 1, 365),
            max_messages=r.integer("INVOICEBOT_MAIL_MAX_MESSAGES", 200, 1, 5000),
            timeout_s=r.integer("INVOICEBOT_MAIL_TIMEOUT_MS", 60_000, 5000, 600_000) / 1000,
            imap=ImapConfig(
                host=r.text("INVOICEBOT_MAIL_IMAP_HOST"),
                port=r.integer("INVOICEBOT_MAIL_IMAP_PORT", 993, 1, 65535),
                tls=r.flag("INVOICEBOT_MAIL_IMAP_TLS", True),
            ),
            google=GoogleMailConfig(
                client_id=r.text("INVOICEBOT_MAIL_GOOGLE_CLIENT_ID"),
                client_secret_set=r.is_set("INVOICEBOT_MAIL_GOOGLE_CLIENT_SECRET"),
                service_account_file=_resolve(
                    cwd, r.text("INVOICEBOT_MAIL_GOOGLE_SERVICE_ACCOUNT_FILE")
                ),
            ),
            microsoft=MicrosoftMailConfig(
                tenant=tenant,
                client_id=r.text("INVOICEBOT_MAIL_MS_CLIENT_ID"),
                client_secret_set=ms_secret,
                cert_file=ms_cert,
            ),
            refresh_token_set=r.is_set("INVOICEBOT_MAIL_REFRESH_TOKEN"),
            token_file=str(Path(data_dir) / "mail-token.json"),
        ),
        data_dir=data_dir,
        db=DbConfig(
            connection_string=r.text("INVOICEBOT_DB_CONNECTION_STRING"),
            driver=r.text("INVOICEBOT_DB_DRIVER", "ODBC Driver 18 for SQL Server")
            or "ODBC Driver 18 for SQL Server",
            server=db_server,
            port=db_port,
            name=overrides.get("db_name")
            or r.text("INVOICEBOT_DB_NAME", "invoicebot")
            or "invoicebot",
            auth=r.choice(
                "INVOICEBOT_DB_AUTH", db_auth_default, ("sql", "windows", "managed-identity")
            ),
            user=db_user,
            password_set=r.is_set("INVOICEBOT_DB_PASSWORD"),
            managed_identity_client_id=r.text(
                "INVOICEBOT_DB_MI_CLIENT_ID", r.text("AZURE_CLIENT_ID")
            ),
            encrypt=r.flag("INVOICEBOT_DB_ENCRYPT", True),
            trust_server_certificate=r.flag("INVOICEBOT_DB_TRUST_CERT", False),
            pool_size=r.integer("INVOICEBOT_DB_POOL_MAX", 10, 1, 100),
            windows_login=r.text("INVOICEBOT_DB_WINDOWS_LOGIN"),
            auto_migrate=r.flag("INVOICEBOT_AUTO_MIGRATE", True),
        ),
        export_dir=export_dir,
        export_format=r.choice("INVOICEBOT_EXPORT_FORMAT", "csv", ("csv", "xlsx")),
        upload_dir=_resolve(cwd, r.text("INVOICEBOT_UPLOAD_DIR"))
        or str(Path(inbox_dir or data_dir) / "api"),
        concurrency=int(
            overrides.get("concurrency") or r.integer("INVOICEBOT_CONCURRENCY", 2, 1, 16)
        ),
        max_attempts=r.integer("INVOICEBOT_MAX_ATTEMPTS", 3, 1, 10),
        min_text_chars=r.integer("INVOICEBOT_MIN_TEXT_CHARS", 40, 0, 100_000),
        amount_tolerance_cents=tolerance_cents,
        log=LogConfig(
            level=r.choice("INVOICEBOT_LOG_LEVEL", "warning", (*LOG_LEVELS, "warn", "silent")),
            dir=_resolve(cwd, r.text("INVOICEBOT_LOG_DIR")),
            retention_days=r.integer("INVOICEBOT_LOG_RETENTION_DAYS", 30, 1, 3650),
            format=r.choice("INVOICEBOT_LOG_FORMAT", "text", ("text", "json")),
        ),
        backup=BackupConfig(
            dir=r.text("INVOICEBOT_BACKUP_DIR"),
            retention=r.integer("INVOICEBOT_BACKUP_RETENTION", 14, 1, 1000),
            interval_hours=r.integer("INVOICEBOT_BACKUP_INTERVAL_HOURS", 24, 1, 24 * 365),
        ),
        ocr=OcrConfig(
            engine=r.choice("INVOICEBOT_OCR_ENGINE", "tesseract", ("tesseract", "claude", "none")),
            languages=ocr_languages or ("spa", "eng"),
            max_pages=r.integer("INVOICEBOT_OCR_MAX_PAGES", 3, 1, 20),
            scale=r.number("INVOICEBOT_OCR_SCALE", 3.0, 1.0, 6.0),
            min_confidence=r.integer("INVOICEBOT_OCR_MIN_CONFIDENCE", 70, 0, 100),
            tesseract_cmd=r.text("INVOICEBOT_OCR_TESSERACT_CMD"),
        ),
        serve=ServeConfig(
            host=r.text("INVOICEBOT_SERVE_HOST", "127.0.0.1") or "127.0.0.1",
            port=r.integer("INVOICEBOT_SERVE_PORT", 4317, 1, 65535),
            tls_cert=tls_cert,
            tls_key=tls_key,
            session_hours=r.integer("INVOICEBOT_SERVE_SESSION_HOURS", 10, 1, 24 * 30),
            cookie_secure=r.choice(
                "INVOICEBOT_SERVE_COOKIE_SECURE", "auto", ("auto", "true", "false")
            ),
            forwarded_allow_ips=r.text("INVOICEBOT_FORWARDED_ALLOW_IPS", "127.0.0.1")
            or "127.0.0.1",
            api_docs=r.flag("INVOICEBOT_API_DOCS", True),
            upload_max_mb=r.integer("INVOICEBOT_UPLOAD_MAX_MB", 25, 1, 500),
        ),
        run=RunConfig(
            inbox_dir=inbox_dir,
            recursive=r.flag("INVOICEBOT_INBOX_RECURSIVE", True),
            export=r.choice("INVOICEBOT_RUN_EXPORT", "none", ("none", "csv", "xlsx")),
            interval_minutes=run_interval,
            partial_exit_code=r.integer("INVOICEBOT_RUN_PARTIAL_EXIT_CODE", 3, 0, 255),
        ),
        queue=QueueConfig(
            backend=queue_backend,
            redis_url_set=redis_url_set,
            namespace=namespace,
            worker_threads=r.integer("INVOICEBOT_WORKER_THREADS", 4, 1, 64),
            ai_rate_per_minute=r.integer("INVOICEBOT_AI_RATE_PER_MINUTE", 0, 0, 100_000),
            sunat_rate_per_minute=r.integer("INVOICEBOT_SUNAT_RATE_PER_MINUTE", 30, 0, 100_000),
            job_max_attempts=r.integer("INVOICEBOT_JOB_MAX_ATTEMPTS", 5, 1, 50),
        ),
        # Con colas, el ciclo completo se reparte en tareas más pequeñas: por defecto se
        # programan esas tareas y no el ciclo (que sigue disponible para Windows).
        schedule=ScheduleConfig(
            cycle=cron("INVOICEBOT_SCHEDULE_CYCLE", None),
            collect=cron("INVOICEBOT_SCHEDULE_COLLECT", f"*/{min(run_interval, 59)} * * * *"),
            fx=cron("INVOICEBOT_SCHEDULE_FX", "5 8,13,18 * * *"),
            sunat=cron("INVOICEBOT_SCHEDULE_SUNAT", "30 */2 * * *"),
            retry=cron("INVOICEBOT_SCHEDULE_RETRY", "*/30 * * * *"),
            export=cron("INVOICEBOT_SCHEDULE_EXPORT", "0 6 * * *"),
            backup=cron("INVOICEBOT_SCHEDULE_BACKUP", "0 2 * * *"),
            purge=cron("INVOICEBOT_SCHEDULE_PURGE", "30 3 * * *"),
            job_retention_days=r.integer("INVOICEBOT_JOB_RETENTION_DAYS", 30, 1, 3650),
        ),
        timezone=timezone,
        reviewer=overrides.get("reviewer") or r.text("INVOICEBOT_USER"),
        env=env,
    )
    if r.errors:
        raise ConfigError("Configuración no válida:\n  - " + "\n  - ".join(r.errors))
    return config
