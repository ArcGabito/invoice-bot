"""Contexto de la aplicación: crea cada dependencia al pedirla (CLI, API y Job la comparten)."""

from __future__ import annotations

import getpass
import logging
import threading
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any

from invoicebot.application.deps import Deps
from invoicebot.application.ports import AiProvider, MailSource, VisionExtractor
from invoicebot.application.run_bot import BackupStep, BotRunResult, run_bot
from invoicebot.config import Config, load_config
from invoicebot.errors import ConfigError
from invoicebot.infrastructure.db.backup import create_backup, is_backup_due
from invoicebot.infrastructure.db.connection import Database
from invoicebot.infrastructure.db.invoice_repository import InvoiceRepository
from invoicebot.infrastructure.db.lock import run_lock
from invoicebot.infrastructure.db.migration import MigrationResult, run_migrations, schema_status
from invoicebot.infrastructure.db.user_repository import UserRepository
from invoicebot.infrastructure.documents.router import DocumentRouter
from invoicebot.logging_setup import configure_logging

if TYPE_CHECKING:
    from invoicebot.application.jobs import JobQueue, Jobs
    from invoicebot.infrastructure.db.company_repository import CompanyRepository
    from invoicebot.infrastructure.db.job_repository import JobRepository
    from invoicebot.infrastructure.queue import RateLimiter


class AppContext:
    def __init__(
        self,
        config: Config | None = None,
        *,
        verbose: bool = False,
        reviewer: str | None = None,
        logger: logging.Logger | None = None,
    ):
        self._config = config
        self._verbose = verbose
        self._reviewer = reviewer
        self._logger = logger
        self._schema_checked = False
        self._schema_lock = threading.Lock()
        self._limiters: dict[str, RateLimiter] = {}
        self._limiters_lock = threading.Lock()

    # --- Configuración y logs ---------------------------------------------------

    @cached_property
    def config(self) -> Config:
        return self._config or load_config()

    @cached_property
    def logger(self) -> logging.Logger:
        return self._logger or configure_logging(self.config.log, verbose=self._verbose)

    @property
    def reviewer(self) -> str:
        """Quién firma las revisiones desde la CLI: --user, INVOICEBOT_USER o el usuario del sistema."""
        if self._reviewer or self.config.reviewer:
            return str(self._reviewer or self.config.reviewer)
        try:
            return getpass.getuser()
        except (OSError, KeyError):
            return "invoicebot"

    # --- Base de datos ----------------------------------------------------------

    @cached_property
    def db(self) -> Database:
        return Database.from_config(self.config.db, self.config.env)

    def ensure_schema(self, *, migrate: bool | None = None) -> MigrationResult | None:
        """Aplica las migraciones pendientes (con copia previa) una vez por proceso.

        Con ``migrate=None`` decide INVOICEBOT_AUTO_MIGRATE; los workers pasan False.
        """
        if migrate is None:
            migrate = self.config.db.auto_migrate
        with self._schema_lock:
            if self._schema_checked:
                return None
            status = schema_status(self.db)
            if status["ahead"]:
                raise ConfigError(
                    f"La base de datos tiene el esquema v{status['current']}, más nuevo que el de "
                    f"esta versión de InvoiceBot (v{status['latest']}): actualiza InvoiceBot"
                )
            result = None
            if status["pending"]:
                if not migrate:
                    raise ConfigError(
                        "La base de datos necesita migraciones: ejecuta `invoicebot db migrate`"
                    )
                result = run_migrations(self.db, backup=self._migration_backup)
                if result.applied:
                    self.logger.info("migraciones aplicadas: %s", ", ".join(result.applied))
            self._schema_checked = True
            return result

    def _migration_backup(self, label: str) -> str | None:
        if self.db.is_azure_sql():
            return None
        return create_backup(self.db, self.config.backup.dir, label=label).path

    @cached_property
    def repository(self) -> InvoiceRepository:
        self.ensure_schema()
        return InvoiceRepository(self.db)

    @cached_property
    def companies(self) -> CompanyRepository:
        from invoicebot.infrastructure.db.company_repository import CompanyRepository

        self.ensure_schema()
        return CompanyRepository(self.db)

    @cached_property
    def users(self) -> UserRepository:
        self.ensure_schema()
        return UserRepository(self.db)

    # --- Documentos e IA --------------------------------------------------------

    @cached_property
    def document_router(self) -> DocumentRouter:
        from invoicebot.infrastructure.ocr import create_ocr_engine

        return DocumentRouter(
            min_text_chars=self.config.min_text_chars, ocr_engine=create_ocr_engine(self.config)
        )

    @cached_property
    def ai_provider(self) -> AiProvider:
        from invoicebot.infrastructure.ai.factory import create_ai_provider

        return create_ai_provider(self.config)

    @cached_property
    def vision_extractor(self) -> VisionExtractor | None:
        from invoicebot.infrastructure.ai.factory import create_vision_extractor

        return create_vision_extractor(self.config)  # type: ignore[return-value]

    @property
    def deps(self) -> Deps:
        return Deps(
            config=self.config,
            repository=self.repository,
            document_router=self.document_router,
            ai_provider=self.ai_provider,
            vision_extractor=self.vision_extractor,
            today=self.config.today(),
            logger=self.logger,
        )

    def review_deps(self) -> Deps:
        """Para revisar no hacen falta IA ni OCR: no se crean (ni se validan sus credenciales)."""
        return Deps(
            config=self.config,
            repository=self.repository,
            document_router=_NoRouter(),
            ai_provider=_NoAi(),
            vision_extractor=None,
            today=self.config.today(),
            logger=self.logger,
        )

    # --- Servicios externos -----------------------------------------------------

    def sunat_client(self) -> Any:
        from invoicebot.infrastructure.sunat import create_sunat_client

        return create_sunat_client(self.config)

    @cached_property
    def oidc(self) -> Any:
        """Cliente OpenID Connect (INVOICEBOT_OIDC_*), o ConfigError si no está configurado."""
        from invoicebot.infrastructure.oidc import OidcClient

        settings = self.config.oidc
        if not settings.enabled:
            raise ConfigError("El inicio de sesión único no está configurado (INVOICEBOT_OIDC_*)")
        return OidcClient(
            issuer=str(settings.issuer),
            client_id=str(settings.client_id),
            client_secret=self.config.secret("INVOICEBOT_OIDC_CLIENT_SECRET"),
            scopes=settings.scopes,
        )

    def mail_source(self) -> MailSource:
        from invoicebot.infrastructure.mail.factory import create_mail_source

        return create_mail_source(self.config)

    def mail_dir(self) -> str:
        if self.config.mail.dir:
            return self.config.mail.dir
        inbox = self.config.run.inbox_dir
        if not inbox:
            raise ConfigError(
                "Define INVOICEBOT_INBOX_DIR (o INVOICEBOT_MAIL_DIR) para guardar los adjuntos"
            )
        return str(Path(inbox) / "correo")

    # --- Trabajos en cola ---------------------------------------------------------

    @cached_property
    def job_repository(self) -> JobRepository:
        from invoicebot.infrastructure.db.job_repository import JobRepository

        self.ensure_schema()
        return JobRepository(self.db)

    @cached_property
    def redis(self) -> Any:
        """Cliente de Redis (INVOICEBOT_REDIS_URL); None con la cola inline."""
        url = self.config.secret("INVOICEBOT_REDIS_URL")
        if not url:
            return None
        from invoicebot.infrastructure.queue.redis_queue import create_redis_client

        return create_redis_client(url)

    @cached_property
    def queue(self) -> JobQueue:
        from invoicebot.application.tasks import registry

        if self.config.queue.backend == "redis":
            from invoicebot.infrastructure.queue.redis_queue import RedisQueue

            if self.redis is None:
                raise ConfigError("INVOICEBOT_QUEUE_BACKEND=redis requiere INVOICEBOT_REDIS_URL")
            redis_queue = RedisQueue(
                client=self.redis, namespace=self.config.queue.namespace, registry=registry
            )
            redis_queue.bind(self._execute_job)
            return redis_queue
        from invoicebot.infrastructure.queue import InlineQueue

        return InlineQueue(self._execute_job)

    def _execute_job(self, job_id: int) -> str:
        return self.jobs.execute(job_id)

    @cached_property
    def jobs(self) -> Jobs:
        from invoicebot.application.jobs import Jobs
        from invoicebot.application.tasks import registry

        return Jobs(
            repository=self.job_repository,
            queue=self.queue,
            registry=registry,
            context=self,
            default_max_attempts=self.config.queue.job_max_attempts,
            logger=self.logger,
        )

    def dispatch_document(self, document: dict[str, Any]) -> None:
        """Encola el procesamiento de un documento (una sola vez mientras siga activo)."""
        document_id = int(document["id"])
        self.jobs.enqueue(
            "documents.process",
            {"document_id": document_id},
            dedupe_key=f"documents.process:{document_id}",
        )

    def rate_limiter(self, name: str) -> RateLimiter:
        """Límite de llamadas por minuto compartido por todos los workers (`ai` o `sunat`)."""
        from invoicebot.infrastructure.queue import RateLimiter

        with self._limiters_lock:
            if name not in self._limiters:
                per_minute = {
                    "ai": self.config.queue.ai_rate_per_minute,
                    "sunat": self.config.queue.sunat_rate_per_minute,
                }[name]
                self._limiters[name] = RateLimiter(
                    f"{self.config.queue.namespace}:rate:{name}",
                    per_minute,
                    redis_client=self.redis,
                )
            return self._limiters[name]

    @cached_property
    def _limited_ai(self) -> tuple[Any, Any]:
        from invoicebot.infrastructure.queue.limits import RateLimited

        limiter = self.rate_limiter("ai")
        vision = self.vision_extractor
        return (
            RateLimited(self.ai_provider, limiter, ("extract",)),
            RateLimited(vision, limiter, ("extract", "extract_from_file")) if vision else None,
        )

    @property
    def worker_deps(self) -> Deps:
        """Dependencias de las tareas: la IA y la visión pasan por el límite de llamadas.

        Es una propiedad (no se guarda): un worker vive días y la fecha cambia.
        """
        ai, vision = self._limited_ai
        return Deps(
            config=self.config,
            repository=self.repository,
            document_router=self.document_router,
            ai_provider=ai,
            vision_extractor=vision,
            today=self.config.today(),
            logger=self.logger,
        )

    def limited_sunat_client(self) -> Any:
        from invoicebot.infrastructure.queue.limits import RateLimited

        return RateLimited(self.sunat_client(), self.rate_limiter("sunat"), ("validate_receipt",))

    # --- Ciclo RPA --------------------------------------------------------------

    def run_cycle(self, *, trigger: str = "manual", inbox_dir: str | None = None) -> BotRunResult:
        """Un ciclo completo con el candado de SQL Server (LockedError si ya hay uno)."""
        config = self.config
        inbox = inbox_dir or config.run.inbox_dir
        if not inbox:
            raise ConfigError("Define INVOICEBOT_INBOX_DIR: la carpeta de entrada del bot")
        deps = self.deps
        with run_lock(self.db):
            closed = self.repository.close_abandoned_bot_runs()
            if closed:
                self.logger.warning("se cerraron %s ejecuciones abandonadas", closed)
            from invoicebot.infrastructure.sunat import (
                SUNAT_EXCHANGE_RATE_URL,
                SunatExchangeRateSource,
            )

            fx = (
                SunatExchangeRateSource(
                    url=config.fx.sunat_url or SUNAT_EXCHANGE_RATE_URL,
                    timeout_s=config.fx.timeout_s,
                )
                if config.fx.sunat
                else None
            )
            sunat = (
                self.sunat_client()
                if config.sunat.enabled and config.sunat.verify_on_scan
                else None
            )
            mail = (self.mail_source(), self.mail_dir()) if config.mail.provider != "none" else None
            backup = None
            if not self.db.is_azure_sql():
                backup = BackupStep(
                    is_due=lambda: is_backup_due(self.db, config.backup.interval_hours),
                    create=lambda: (
                        create_backup(
                            self.db, config.backup.dir, retention=config.backup.retention
                        ).path
                    ),
                )
            return run_bot(
                deps,
                inbox_dir=inbox,
                recursive=config.run.recursive,
                trigger=trigger,
                sunat_client=sunat,
                exchange_rates=fx,
                mail=mail,
                export_format=config.run.export,
                backup=backup,
            )

    def close(self) -> None:
        if "db" in self.__dict__:
            self.db.dispose()


class _NoRouter:
    can_ocr = False

    def extract_text(self, **_: Any) -> Any:
        raise ConfigError("La lectura de documentos no está disponible en este contexto")


class _NoAi:
    name = "none"
    model = "none"

    def extract(self, **_: Any) -> Any:
        raise ConfigError("La IA no está disponible en este contexto")

    def ping(self) -> str:
        return "sin IA"
