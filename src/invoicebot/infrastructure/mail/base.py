"""Buzón de correo: errores comunes a los conectores."""

from __future__ import annotations

from invoicebot.errors import InvoiceBotError

#: Tamaño máximo de un mensaje que se descarga (los proveedores limitan a 25-35 MB).
MAX_MESSAGE_BYTES = 50 * 1024 * 1024


class MailError(InvoiceBotError):
    """Fallo al hablar con el servidor de correo.

    `auth`: credenciales rechazadas o vencidas (hay que corregir la configuración
    o volver a iniciar sesión): el ciclo no sigue con el resto de mensajes.
    """

    def __init__(self, message: str, *, auth: bool = False):
        super().__init__(message, code="MAIL_AUTH_ERROR" if auth else "MAIL_ERROR")
        self.auth = auth
