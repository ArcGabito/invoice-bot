"""Conectores de buzón: IMAP, API de Gmail y Microsoft Graph.

Todos son de solo lectura salvo que se configure ordenar el buzón
(INVOICEBOT_MAIL_PROCESSED_FOLDER / _REVIEW_FOLDER), que exige permiso de
escritura. InvoiceBot nunca borra mensajes.
"""

from __future__ import annotations

import base64
import imaplib
import re
from contextlib import suppress
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from invoicebot.application.ports import MailMessageRef
from invoicebot.infrastructure.mail.base import MAX_MESSAGE_BYTES, MailError
from invoicebot.infrastructure.mail.http import MailApi
from invoicebot.infrastructure.mail.oauth import TokenProvider

GMAIL_API_URL = "https://gmail.googleapis.com/gmail/v1"
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
#: Para mover mensajes (poner etiquetas). No permite borrar.
GMAIL_MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
GRAPH_API_URL = "https://graph.microsoft.com/v1.0"
MICROSOFT_LOGIN_URL = "https://login.microsoftonline.com"
GRAPH_DELEGATED_SCOPE = "offline_access https://graph.microsoft.com/Mail.Read"
GRAPH_DELEGATED_WRITE_SCOPE = "offline_access https://graph.microsoft.com/Mail.ReadWrite"
GRAPH_APP_SCOPE = "https://graph.microsoft.com/.default"
_MAX_LISTED = 5000
_WELL_KNOWN_FOLDERS = frozenset({"inbox", "archive", "junkemail", "sentitems", "deleteditems"})

_KNOWN_IMAP_HOSTS = {
    "gmail.com": "imap.gmail.com",
    "googlemail.com": "imap.gmail.com",
    "yahoo.com": "imap.mail.yahoo.com",
    "zoho.com": "imap.zoho.com",
    "outlook.com": "outlook.office365.com",
    "hotmail.com": "outlook.office365.com",
}


def default_imap_host(user: str | None) -> str | None:
    domain = (user or "").rpartition("@")[2].lower()
    return _KNOWN_IMAP_HOSTS.get(domain)


def microsoft_token_url(tenant: str, login_url: str = MICROSOFT_LOGIN_URL) -> str:
    return f"{login_url}/{quote(tenant, safe='')}/oauth2/v2.0/token"


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def has_attachment_structure(bodystructure: str) -> bool:
    """¿La BODYSTRUCTURE de IMAP tiene una parte adjunta (y no solo el cuerpo)?"""
    upper = bodystructure.upper()
    if '("ATTACHMENT"' in upper:
        return True
    # Partes con nombre de archivo que no son texto (p. ej. "APPLICATION" "PDF" ("NAME" …)).
    return bool(re.search(r'\("(?!TEXT")[A-Z]+" "[^"]+" \([^)]*"(NAME|FILENAME)"', upper))


def _quote_imap(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


class ImapSource:
    """IMAP (Gmail con contraseña de aplicación, hosting, Zoho, Yahoo…)."""

    provider = "imap"

    def __init__(
        self,
        *,
        host: str,
        user: str,
        password: str,
        port: int = 993,
        tls: bool = True,
        folder: str | None = None,
        writable: bool = False,
        timeout_s: float = 60.0,
    ):
        self.host, self.port, self.tls = host, port, tls
        self.user, self.password = user, password
        self.folder = folder or "INBOX"
        self.writable = writable
        self.timeout_s = timeout_s
        self._imap: imaplib.IMAP4 | None = None
        self._uid_validity = ""

    def _open(self) -> imaplib.IMAP4:
        if self._imap is None:
            raise MailError("IMAP: no hay conexión abierta")
        return self._imap

    def connect(self) -> str:
        try:
            if self.tls:
                self._imap = imaplib.IMAP4_SSL(self.host, self.port, timeout=self.timeout_s)
            else:
                self._imap = imaplib.IMAP4(self.host, self.port, timeout=self.timeout_s)
                self._imap.starttls()
        except (TimeoutError, OSError, imaplib.IMAP4.error) as error:
            self._imap = None
            raise MailError(
                f"No se pudo conectar con el servidor IMAP {self.host}:{self.port}: {error}"
            ) from error
        try:
            self._imap.login(self.user, self.password)
        except imaplib.IMAP4.error as error:
            self.close()
            raise MailError(
                f"El servidor IMAP {self.host} rechazó el usuario o la contraseña de {self.user}. "
                "En Gmail usa una contraseña de aplicación (requiere verificación en dos pasos).",
                auth=True,
            ) from error
        status, _ = self._imap.select(_quote_imap(self.folder), readonly=not self.writable)
        if status != "OK":
            raise MailError(f'No se pudo abrir la carpeta "{self.folder}"')
        _, data = self._imap.response("UIDVALIDITY")
        self._uid_validity = (data[0].decode() if data and data[0] else "") or "0"
        return self.user

    def list_messages(self, *, since: datetime) -> list[MailMessageRef]:
        imap = self._open()
        status, data = imap.uid("SEARCH", None, "SINCE", since.strftime("%d-%b-%Y"))  # type: ignore[arg-type]
        if status != "OK" or not data or not data[0]:
            return []
        uids = data[0].split()
        refs: list[MailMessageRef] = []
        for i in range(0, len(uids), 200):
            chunk = b",".join(uids[i : i + 200]).decode()
            status, items = imap.uid("FETCH", chunk, "(UID INTERNALDATE BODYSTRUCTURE)")
            if status != "OK":
                continue
            for item in items:
                line = (
                    item.decode(errors="replace")
                    if isinstance(item, bytes)
                    else (
                        b" ".join(p for p in item if isinstance(p, bytes)).decode(errors="replace")
                        if isinstance(item, tuple)
                        else ""
                    )
                )
                uid = re.search(r"UID (\d+)", line)
                if not uid or not has_attachment_structure(line):
                    continue
                internal = re.search(r'INTERNALDATE "([^"]+)"', line)
                received = None
                if internal:
                    try:
                        received = datetime.strptime(
                            internal[1], "%d-%b-%Y %H:%M:%S %z"
                        ).astimezone(UTC)
                    except ValueError:
                        received = None
                refs.append(MailMessageRef(f"{self._uid_validity}:{uid[1]}", received))
        return sorted(refs, key=lambda r: r.received_at or datetime.min.replace(tzinfo=UTC))

    def _uid(self, message_id: str) -> str:
        validity, _, uid = message_id.partition(":")
        if validity != self._uid_validity or not uid:
            raise MailError(
                f"El mensaje {message_id} ya no existe en la carpeta (UIDVALIDITY cambió)"
            )
        return uid

    def fetch_raw(self, message_id: str) -> bytes:
        status, data = self._open().uid("FETCH", self._uid(message_id), "(BODY.PEEK[])")
        raw = (
            next((p[1] for p in data if isinstance(p, tuple) and len(p) > 1), None)
            if status == "OK"
            else None
        )
        if not raw:
            raise MailError(f"El mensaje {message_id} ya no existe")
        if len(raw) > MAX_MESSAGE_BYTES:
            raise MailError(f"Mensaje {message_id} demasiado grande")
        return bytes(raw)

    def move(self, message_id: str, folder: str) -> None:
        imap = self._open()
        uid = self._uid(message_id)
        imap.create(_quote_imap(folder))  # Si ya existe, el servidor responde NO y se sigue.
        status, _ = imap.uid("MOVE", uid, _quote_imap(folder))
        if status != "OK":
            # Servidores sin MOVE (RFC 6851): copiar y marcar como borrado en origen.
            status, _ = imap.uid("COPY", uid, _quote_imap(folder))
            if status != "OK":
                raise MailError(f'El servidor IMAP no movió el mensaje {message_id} a "{folder}"')
            imap.uid("STORE", uid, "+FLAGS.SILENT", r"(\Deleted)")
            imap.expunge()

    def close(self) -> None:
        imap, self._imap = self._imap, None
        if imap is None:
            return
        with suppress(OSError, imaplib.IMAP4.error):
            imap.logout()


class GmailSource:
    """API de Gmail (gmail.readonly, o gmail.modify si se ordena el buzón con etiquetas)."""

    provider = "gmail"

    def __init__(
        self,
        *,
        tokens: TokenProvider,
        user_id: str = "me",
        label: str | None = None,
        inbox_only: bool = False,
        api_url: str = GMAIL_API_URL,
        timeout_s: float = 60.0,
        client: httpx.Client | None = None,
    ):
        self.base = f"{api_url}/users/{quote(user_id, safe='@')}"
        self.label = label
        self.inbox_only = inbox_only
        self.api = MailApi(tokens=tokens, service="Gmail", client=client, timeout_s=timeout_s)
        self._labels: dict[str, str] = {}

    def connect(self) -> str:
        profile = self.api.json(f"{self.base}/profile")
        if not profile.get("emailAddress"):
            raise MailError("Gmail no devolvió la dirección del buzón")
        return str(profile["emailAddress"])

    def list_messages(self, *, since: datetime) -> list[MailMessageRef]:
        if self.label:
            where = 'label:"' + self.label.replace('"', "") + '"'
        else:
            where = "in:inbox" if self.inbox_only else "-in:sent -in:drafts"
        q = f"has:attachment {where} after:{int(since.timestamp())}"
        ids: list[str] = []
        page_token: str | None = None
        while True:
            params = {"q": q, "maxResults": "500"}
            if page_token:
                params["pageToken"] = page_token
            page = self.api.json(f"{self.base}/messages?{urlencode(params)}")
            ids.extend(m["id"] for m in page.get("messages", []))
            page_token = page.get("nextPageToken")
            if not page_token or len(ids) >= _MAX_LISTED:
                break
        # Gmail lista del más reciente al más antiguo.
        return [MailMessageRef(i, None) for i in reversed(ids)]

    def fetch_raw(self, message_id: str) -> bytes:
        message = self.api.json(f"{self.base}/messages/{quote(message_id, safe='')}?format=raw")
        raw = message.get("raw")
        if not raw:
            raise MailError(f"Gmail no devolvió el contenido del mensaje {message_id}")
        data = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        if len(data) > MAX_MESSAGE_BYTES:
            raise MailError(f"Mensaje {message_id} demasiado grande")
        return data

    def _label_id(self, name: str) -> str:
        cached = self._labels.get(name.lower())
        if cached:
            return cached
        labels = self.api.json(f"{self.base}/labels").get("labels", [])
        found = next((lab for lab in labels if lab["name"].lower() == name.lower()), None)
        if found is None:
            found = self.api.json(
                f"{self.base}/labels",
                body={
                    "name": name,
                    "labelListVisibility": "labelShow",
                    "messageListVisibility": "show",
                },
            )
        self._labels[name.lower()] = found["id"]
        return str(found["id"])

    def move(self, message_id: str, folder: str) -> None:
        self.api.json(
            f"{self.base}/messages/{quote(message_id, safe='')}/modify",
            body={"addLabelIds": [self._label_id(folder)], "removeLabelIds": ["INBOX"]},
        )

    def close(self) -> None:
        return None


class GraphSource:
    """Microsoft 365 / Outlook.com por Microsoft Graph.

    `mailbox` None: el del usuario que inició sesión (/me); con dirección: ese buzón,
    con permiso de aplicación.
    """

    provider = "graph"

    def __init__(
        self,
        *,
        tokens: TokenProvider,
        mailbox: str | None = None,
        folder: str | None = None,
        api_url: str = GRAPH_API_URL,
        timeout_s: float = 60.0,
        client: httpx.Client | None = None,
    ):
        self.mailbox = mailbox
        self.root = f"{api_url}/users/{quote(mailbox, safe='@')}" if mailbox else f"{api_url}/me"
        self.folder = folder
        self.api = MailApi(
            tokens=tokens, service="Microsoft Graph", client=client, timeout_s=timeout_s
        )
        self._folder_id: str | None = None
        self._folders: dict[str, str] = {}

    def _find_folder(self, name: str) -> str | None:
        if name.lower() in _WELL_KNOWN_FOLDERS:
            return name.lower()
        escaped = name.replace("'", "''")
        query = urlencode({"$filter": f"displayName eq '{escaped}'", "$select": "id"})
        for parent in (f"{self.root}/mailFolders", f"{self.root}/mailFolders/inbox/childFolders"):
            found = self.api.json(f"{parent}?{query}").get("value", [])
            if found:
                return str(found[0]["id"])
        return None

    def _resolve_folder(self) -> str:
        if self._folder_id:
            return self._folder_id
        name = self.folder or "inbox"
        self._folder_id = self._find_folder(name)
        if not self._folder_id:
            raise MailError(f'No existe la carpeta "{name}" en el buzón')
        return self._folder_id

    def connect(self) -> str:
        if self.mailbox:
            # Comprueba que la aplicación tiene acceso a ese buzón.
            self.api.request(f"{self.root}/mailFolders/inbox?$select=id")
            self._resolve_folder()
            return self.mailbox
        me = self.api.json(f"{self.root}?$select=mail,userPrincipalName")
        self._resolve_folder()
        address = me.get("mail") or me.get("userPrincipalName")
        if not address:
            raise MailError("Microsoft Graph no devolvió la dirección del buzón")
        return str(address)

    def list_messages(self, *, since: datetime) -> list[MailMessageRef]:
        since_iso = since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        params = urlencode(
            {
                "$filter": f"hasAttachments eq true and receivedDateTime ge {since_iso}",
                "$select": "id,receivedDateTime",
                "$top": "100",
            }
        )
        url: str | None = (
            f"{self.root}/mailFolders/{quote(self._resolve_folder(), safe='')}/messages?{params}"
        )
        refs: list[MailMessageRef] = []
        while url and len(refs) < _MAX_LISTED:
            page: dict[str, Any] = self.api.json(url)
            refs.extend(
                MailMessageRef(m["id"], _parse_iso(m.get("receivedDateTime")))
                for m in page.get("value", [])
            )
            url = page.get("@odata.nextLink")
        return sorted(refs, key=lambda r: r.received_at or datetime.min.replace(tzinfo=UTC))

    def fetch_raw(self, message_id: str) -> bytes:
        res = self.api.request(f"{self.root}/messages/{quote(message_id, safe='')}/$value")
        if len(res.content) > MAX_MESSAGE_BYTES:
            raise MailError(f"Mensaje {message_id} demasiado grande")
        return res.content

    def _destination(self, name: str) -> str:
        folder_id = self._folders.get(name.lower()) or self._find_folder(name)
        if folder_id is None:
            folder_id = str(
                self.api.json(
                    f"{self.root}/mailFolders/inbox/childFolders", body={"displayName": name}
                )["id"]
            )
        self._folders[name.lower()] = folder_id
        return folder_id

    def move(self, message_id: str, folder: str) -> None:
        self.api.json(
            f"{self.root}/messages/{quote(message_id, safe='')}/move",
            body={"destinationId": self._destination(folder)},
        )

    def close(self) -> None:
        return None


def parse_rfc2822(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
