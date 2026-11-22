"""Descubrimiento de archivos compatibles y SHA-256 por streaming."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

SUPPORTED_EXTENSIONS: dict[str, str] = {
    ".xml": "application/xml",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}
XML_MIME_TYPE = "application/xml"


@dataclass(frozen=True)
class DiscoveredFile:
    file_path: str
    mime_type: str


@dataclass
class Discovery:
    supported: list[DiscoveredFile] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)


def mime_type_for(path: str | os.PathLike[str]) -> str | None:
    return SUPPORTED_EXTENSIONS.get(Path(path).suffix.lower())


def discover_files(root: str | os.PathLike[str], *, recursive: bool = False) -> Discovery:
    """Rutas absolutas y ordenadas (resultado reproducible), separando las no compatibles."""
    base = Path(root)
    if not base.exists():
        raise FileNotFoundError(f"No existe la ruta: {root}")
    base = base.resolve()
    if base.is_file():
        files = [base]
    else:
        iterator = base.rglob("*") if recursive else base.iterdir()
        files = sorted((p for p in iterator if p.is_file()), key=lambda p: str(p).lower())
    discovery = Discovery()
    for path in files:
        mime = mime_type_for(path)
        if mime:
            discovery.supported.append(DiscoveredFile(str(path), mime))
        else:
            discovery.unsupported.append(str(path))
    return discovery


def hash_file(path: str | os.PathLike[str]) -> tuple[str, int]:
    """(sha256 hex, tamaño en bytes) sin cargar el archivo completo en memoria."""
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while chunk := handle.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size
