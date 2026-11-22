"""Contexto de la aplicación: crea cada dependencia al pedirla (CLI, API y Job la comparten)."""

from __future__ import annotations

import getpass
import logging
import threading
from functools import cached_property
from pathlib import Path
from typing import Any

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

    def ensure_schema(self, *, migrate: bool = True) -> MigrationResult | None:
        """Aplica las migraciones pendientes (con copia previa) una vez por proceso."""
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
