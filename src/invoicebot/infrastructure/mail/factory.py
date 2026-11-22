"""Crea el conector de correo configurado (INVOICEBOT_MAIL_PROVIDER)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from invoicebot.application.ports import MailSource
from invoicebot.config import Config, MailConfig
from invoicebot.errors import ConfigError
from invoicebot.infrastructure.mail.base import MailError
from invoicebot.infrastructure.mail.login import GOOGLE_TOKEN_URL, read_mail_token, write_mail_token
from invoicebot.infrastructure.mail.oauth import (
    client_certificate_provider,
    client_credentials_provider,
    google_service_account_provider,
    read_certificate_pem,
    refresh_token_provider,
)
from invoicebot.infrastructure.mail.sources import (
    GMAIL_API_URL,
    GMAIL_MODIFY_SCOPE,
    GMAIL_SCOPE,
    GRAPH_API_URL,
    GRAPH_APP_SCOPE,
    GRAPH_DELEGATED_SCOPE,
    GRAPH_DELEGATED_WRITE_SCOPE,
    MICROSOFT_LOGIN_URL,
    GmailSource,
    GraphSource,
    ImapSource,
    default_imap_host,
    microsoft_token_url,
)


@dataclass
class MailEndpoints:
    """Puntos de conexión alternativos, para pruebas con servidores simulados."""

    gmail_api_url: str = GMAIL_API_URL
    google_token_url: str = GOOGLE_TOKEN_URL
    graph_api_url: str = GRAPH_API_URL
    microsoft_login_url: str = MICROSOFT_LOGIN_URL
    client: httpx.Client | None = None


def moves_messages(mail: MailConfig) -> bool:
    """¿Se ordena el buzón tras leer cada mensaje? Exige permiso de escritura."""
    return bool(mail.processed_folder or mail.review_folder)


def login_scope(mail: MailConfig) -> str:
    write = moves_messages(mail)
    if mail.provider == "gmail":
        return GMAIL_MODIFY_SCOPE if write else GMAIL_SCOPE
    return GRAPH_DELEGATED_WRITE_SCOPE if write else GRAPH_DELEGATED_SCOPE


def _needs(values: list[tuple[str, object]], what: str) -> None:
    missing = [name for name, value in values if not value]
    if missing:
        raise ConfigError(f"{what}: falta {', '.join(missing)} en .env")


def _stored_refresh_token(config: Config, provider: str, client_id: str) -> str:
    stored = read_mail_token(config.mail.token_file)
    if stored and stored.get("provider") == provider and stored.get("clientId") == client_id:
        return str(stored["refreshToken"])
    from_env = config.secret("INVOICEBOT_MAIL_REFRESH_TOKEN")
    if from_env:
        return from_env
    raise ConfigError(
        "Falta el acceso al buzón: inicia sesión una vez con `invoicebot mail login` "
        "(o define INVOICEBOT_MAIL_REFRESH_TOKEN en .env)."
    )


def create_mail_source(config: Config, endpoints: MailEndpoints | None = None) -> MailSource:
    mail = config.mail
    ep = endpoints or MailEndpoints()
    write = moves_messages(mail)
    if mail.provider == "none":
        raise ConfigError(
            "La lectura de correo está desactivada. Define INVOICEBOT_MAIL_PROVIDER "
            "(imap, gmail o graph) en .env."
        )
    if mail.provider == "imap":
        host = mail.imap.host or default_imap_host(mail.user)
        _needs(
            [
                ("INVOICEBOT_MAIL_USER", mail.user),
                ("INVOICEBOT_MAIL_PASSWORD", mail.password_set),
                ("INVOICEBOT_MAIL_IMAP_HOST", host),
            ],
            "Correo IMAP",
        )
        assert host and mail.user
        return ImapSource(
            host=host,
            port=mail.imap.port,
            tls=mail.imap.tls,
            user=mail.user,
            password=config.secret("INVOICEBOT_MAIL_PASSWORD") or "",
            folder=mail.folder,
            writable=write,
            timeout_s=mail.timeout_s,
        )
    if mail.provider == "gmail":
        common: dict[str, Any] = {
            "label": mail.folder,
            "inbox_only": write,
            "timeout_s": mail.timeout_s,
            "client": ep.client,
            "api_url": ep.gmail_api_url,
        }
        if mail.google.service_account_file:
            # Google Workspace: cuenta de servicio con delegación de dominio, sin inicio de sesión.
            _needs([("INVOICEBOT_MAIL_USER", mail.user)], "Gmail con cuenta de servicio")
            try:
                key = json.loads(Path(mail.google.service_account_file).read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                raise ConfigError(
                    f"No se pudo leer la clave de la cuenta de servicio {mail.google.service_account_file}: {error}"
                ) from error
            if endpoints is not None:
                key["token_uri"] = ep.google_token_url
            assert mail.user
            return GmailSource(
                user_id=mail.user,
                tokens=google_service_account_provider(
                    key=key,
                    subject=mail.user,
                    scope=GMAIL_MODIFY_SCOPE if write else GMAIL_SCOPE,
                    client=ep.client,
                    timeout_s=mail.timeout_s,
                ),
                **common,
            )
        client_id = mail.google.client_id
        _needs(
            [
                ("INVOICEBOT_MAIL_GOOGLE_CLIENT_ID", client_id),
                ("INVOICEBOT_MAIL_GOOGLE_CLIENT_SECRET", mail.google.client_secret_set),
            ],
            "Gmail",
        )
        assert client_id
        return GmailSource(
            tokens=refresh_token_provider(
                token_url=ep.google_token_url,
                client_id=client_id,
                client_secret=config.secret("INVOICEBOT_MAIL_GOOGLE_CLIENT_SECRET"),
                refresh_token=_stored_refresh_token(config, "gmail", client_id),
                client=ep.client,
                timeout_s=mail.timeout_s,
            ),
            **common,
        )

    # graph
    ms = mail.microsoft
    _needs([("INVOICEBOT_MAIL_MS_CLIENT_ID", ms.client_id)], "Microsoft 365 / Outlook")
    assert ms.client_id
    token_url = microsoft_token_url(ms.tenant, ep.microsoft_login_url)
    graph: dict[str, Any] = {
        "folder": mail.folder,
        "timeout_s": mail.timeout_s,
        "client": ep.client,
        "api_url": ep.graph_api_url,
    }
    if ms.client_secret_set or ms.cert_file:
        # Microsoft 365 de empresa: permiso de aplicación otorgado por el administrador.
        _needs([("INVOICEBOT_MAIL_USER", mail.user)], "Microsoft 365 con permiso de aplicación")
        if ms.tenant.lower() in ("common", "organizations", "consumers"):
            raise ConfigError(
                "Con permiso de aplicación (client secret o certificado) define "
                "INVOICEBOT_MAIL_MS_TENANT con el ID del directorio de la empresa."
            )
        if ms.cert_file:
            try:
                pem = Path(ms.cert_file).read_bytes()
            except OSError as error:
                raise ConfigError(
                    f"No se pudo leer el certificado {ms.cert_file}: {error}"
                ) from error
            try:
                certificate = read_certificate_pem(
                    pem, config.secret("INVOICEBOT_MAIL_MS_CERT_PASSPHRASE")
                )
            except MailError as error:
                raise ConfigError(f"INVOICEBOT_MAIL_MS_CERT_FILE: {error.message}") from error
            tokens = client_certificate_provider(
                token_url=token_url,
                client_id=ms.client_id,
                certificate=certificate,
                scope=GRAPH_APP_SCOPE,
                client=ep.client,
                timeout_s=mail.timeout_s,
            )
        else:
            tokens = client_credentials_provider(
                token_url=token_url,
                client_id=ms.client_id,
                client_secret=config.secret("INVOICEBOT_MAIL_MS_CLIENT_SECRET") or "",
                scope=GRAPH_APP_SCOPE,
                client=ep.client,
                timeout_s=mail.timeout_s,
            )
        return GraphSource(tokens=tokens, mailbox=mail.user, **graph)

    stored = read_mail_token(mail.token_file) or {}
    client_id = ms.client_id

    def on_rotate(refresh: str) -> None:
        write_mail_token(
            mail.token_file,
            provider="graph",
            client_id=client_id,
            account=str(stored.get("account", "")),
            refresh_token=refresh,
        )

    return GraphSource(
        tokens=refresh_token_provider(
            token_url=token_url,
            client_id=client_id,
            refresh_token=_stored_refresh_token(config, "graph", client_id),
            scope=GRAPH_DELEGATED_WRITE_SCOPE if write else GRAPH_DELEGATED_SCOPE,
            on_rotate=on_rotate,
            client=ep.client,
            timeout_s=mail.timeout_s,
        ),
        **graph,
    )
