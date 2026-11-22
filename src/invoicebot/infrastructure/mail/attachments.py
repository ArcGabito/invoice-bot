"""Adjuntos de un mensaje MIME que pueden ser comprobantes: XML, PDF, imágenes y ZIP."""

from __future__ import annotations

import io
import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import PurePosixPath

#: Archivos que InvoiceBot sabe leer. Los ZIP se abren y se toman estos de dentro.
DOCUMENT_EXTENSIONS = frozenset({".xml", ".pdf", ".png", ".jpg", ".jpeg"})
IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg"})
EXTENSION_BY_TYPE = {
    "application/pdf": ".pdf",
    "application/xml": ".xml",
    "text/xml": ".xml",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "application/zip": ".zip",
    "application/x-zip-compressed": ".zip",
}
#: Las imágenes pequeñas suelen ser logos de la firma, no comprobantes.
MIN_IMAGE_BYTES = 20 * 1024
#: Protección contra ZIP que se expanden sin fin.
ZIP_MAX_ENTRIES = 200
ZIP_MAX_FILE_BYTES = 30 * 1024 * 1024
ZIP_MAX_TOTAL_BYTES = 100 * 1024 * 1024

_WINDOWS_RESERVED = re.compile(r"^(con|prn|aux|nul|com\d|lpt\d)$", re.IGNORECASE)


@dataclass
class MailFile:
    name: str
    content: bytes


@dataclass
class ParsedMail:
    sender: str | None
    subject: str | None
    date: object
    files: list[MailFile] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def safe_file_name(name: str, fallback: str = "adjunto") -> str:
    """Nombre seguro: sin rutas ("../../x.pdf" no sale de su carpeta) ni caracteres inválidos."""
    base = posixpath.basename(name.replace("\\", "/"))
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base)
    clean = re.sub(r"^[.\s]+|[.\s]+$", "", clean)
    suffix = PurePosixPath(clean).suffix.lower()
    stem = clean[: len(clean) - len(suffix)] if suffix else clean
    if not stem or _WINDOWS_RESERVED.match(stem):
        stem = fallback
    return f"{stem[:120]}{suffix}"


def _extension(name: str | None, content_type: str | None) -> str:
    suffix = PurePosixPath(name).suffix.lower() if name else ""
    return suffix or EXTENSION_BY_TYPE.get((content_type or "").lower(), "")


def unzip_documents(data: bytes, zip_name: str, skipped: list[str]) -> list[MailFile]:
    """Archivos de un ZIP que InvoiceBot sabe leer (sin carpetas ni __MACOSX)."""
    files: list[MailFile] = []
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as error:
        skipped.append(f"{zip_name}: ZIP dañado ({error})")
        return []
    total = 0
    too_big = False
    with archive:
        for index, info in enumerate(archive.infolist()):
            name = info.filename
            if info.is_dir() or name.startswith("__MACOSX/"):
                continue
            if PurePosixPath(name).suffix.lower() not in DOCUMENT_EXTENSIONS:
                continue
            if index >= ZIP_MAX_ENTRIES or info.file_size > ZIP_MAX_FILE_BYTES:
                too_big = True
                continue
            total += info.file_size
            if total > ZIP_MAX_TOTAL_BYTES:
                too_big = True
                continue
            if info.flag_bits & 0x1:
                skipped.append(f"{zip_name}: {name} está cifrado")
                continue
            try:
                with archive.open(info) as handle:
                    content = handle.read(ZIP_MAX_FILE_BYTES + 1)
            except (zipfile.BadZipFile, RuntimeError, OSError) as error:
                skipped.append(f"{zip_name}: no se pudo leer {name} ({error})")
                continue
            if len(content) > ZIP_MAX_FILE_BYTES:
                too_big = True
                continue
            files.append(MailFile(safe_file_name(name), content))
    if too_big:
        skipped.append(f"{zip_name}: se omitieron archivos por exceder los límites del ZIP")
    return files


def _unique_names(files: list[MailFile]) -> list[MailFile]:
    """ "factura.pdf", "factura-2.pdf"…: dos adjuntos con el mismo nombre no se pisan."""
    used: set[str] = set()
    result = []
    for f in files:
        path = PurePosixPath(f.name)
        stem, suffix = path.stem, path.suffix
        name, n = f.name, 2
        while name.lower() in used:
            name = f"{stem}-{n}{suffix}"
            n += 1
        used.add(name.lower())
        result.append(MailFile(name, f.content))
    return result


def extract_mail_files(raw: bytes) -> ParsedMail:
    """Adjuntos que pueden ser comprobantes, incluido el contenido de los ZIP."""
    message = BytesParser(policy=policy.default).parsebytes(raw)
    assert isinstance(message, EmailMessage)
    skipped: list[str] = []
    files: list[MailFile] = []
    for part in message.walk():
        if part.is_multipart():
            continue
        disposition = (part.get_content_disposition() or "").lower()
        filename = part.get_filename()
        content_type = part.get_content_type()
        if not filename and disposition != "attachment":
            continue  # Cuerpo del mensaje.
        ext = _extension(filename, content_type)
        name = filename or f"adjunto{ext}"
        payload = part.get_payload(decode=True)
        content = payload if isinstance(payload, bytes) else b""
        inline = disposition == "inline" or bool(part.get("Content-ID"))
        if ext == ".zip":
            files.extend(unzip_documents(content, name, skipped))
        elif ext in DOCUMENT_EXTENSIONS and ext not in IMAGE_EXTENSIONS:
            files.append(MailFile(safe_file_name(name), content))
        elif ext in IMAGE_EXTENSIONS:
            # Imágenes: solo adjuntos de verdad, no las incrustadas (logos, firmas).
            if inline:
                continue
            if len(content) >= MIN_IMAGE_BYTES:
                files.append(MailFile(safe_file_name(name), content))
            else:
                skipped.append(f"{name}: imagen demasiado pequeña para ser un comprobante")
        else:
            skipped.append(f"{name}: tipo de archivo no soportado")
    senders = getaddresses([str(message.get("From", ""))])
    sender = senders[0][1] if senders and senders[0][1] else None
    try:
        sent = parsedate_to_datetime(str(message["Date"])) if message.get("Date") else None
    except (TypeError, ValueError):
        sent = None
    subject = str(message.get("Subject")) if message.get("Subject") else None
    return ParsedMail(sender, subject, sent, _unique_names(files), skipped)
