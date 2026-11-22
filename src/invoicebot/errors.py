"""Errores de InvoiceBot y códigos de salida públicos de la CLI.

Los códigos de salida están documentados en el README: cambiarlos rompe a
quienes automatizan InvoiceBot desde otros scripts, tareas programadas o
Container Apps Jobs.
"""

from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    OK = 0
    GENERAL_ERROR = 1
    USAGE_ERROR = 2
    PARTIAL = 3


class InvoiceBotError(Exception):
    code = "INVOICEBOT_ERROR"
    exit_code: int = ExitCode.GENERAL_ERROR

    def __init__(self, message: str, *, code: str | None = None, exit_code: int | None = None):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if exit_code is not None:
            self.exit_code = exit_code


class ConfigError(InvoiceBotError):
    """Configuración o argumentos incorrectos (código de salida 2)."""

    code = "CONFIG_ERROR"
    exit_code = ExitCode.USAGE_ERROR


class ExtractionError(InvoiceBotError):
    """Fallo de extracción de un documento concreto.

    `retryable` indica si repetir la misma operación tiene sentido (red, límite
    de tasa, JSON inválido) o no (documento cifrado, rechazo del modelo).
    """

    code = "EXTRACTION_ERROR"

    def __init__(
        self,
        message: str,
        *,
        kind: str = "unknown",
        retryable: bool = False,
        raw_response: str | None = None,
    ):
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable
        self.raw_response = raw_response


class ReviewError(InvoiceBotError):
    """Acción de revisión no permitida (factura inexistente, sin nota, con errores…)."""

    code = "REVIEW_ERROR"
    exit_code = ExitCode.USAGE_ERROR


class UserError(InvoiceBotError):
    """Alta o cambio de usuario no válido (nombre repetido, contraseña corta…)."""

    code = "USER_ERROR"
    exit_code = ExitCode.USAGE_ERROR


class DocumentReadError(InvoiceBotError):
    """Archivo dañado, cifrado o ilegible."""

    code = "DOCUMENT_READ_ERROR"


class LockedError(InvoiceBotError):
    """Ya hay un ciclo del bot en curso (en esta u otra máquina)."""

    code = "LOCKED"


def error_message(error: BaseException | object) -> str:
    """Mensaje legible de cualquier excepción."""
    if isinstance(error, InvoiceBotError):
        return error.message
    if isinstance(error, BaseException):
        return str(error) or error.__class__.__name__
    return str(error)
