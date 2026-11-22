"""Contraseñas con scrypt: scrypt$N$r$p$sal$hash (base64).

Mismo formato que la versión 0.x: los usuarios existentes conservan su contraseña.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import unicodedata
from functools import lru_cache

_N, _R, _P = 2**15, 8, 1
_KEY_LENGTH = 32
_ALPHABET = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _maxmem(n: int, r: int) -> int:
    return 128 * n * r * 2


def generate_password() -> str:
    """20 caracteres sin ambigüedades (0/O, 1/l), en grupos de 5: fácil de dictar."""
    chars = "".join(secrets.choice(_ALPHABET) for _ in range(20))
    return "-".join(chars[i : i + 5] for i in range(0, 20, 5))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        unicodedata.normalize("NFKC", password).encode(),
        salt=salt,
        n=_N,
        r=_R,
        p=_P,
        dklen=_KEY_LENGTH,
        maxmem=_maxmem(_N, _R),
    )
    return "$".join(
        (
            "scrypt",
            str(_N),
            str(_R),
            str(_P),
            base64.b64encode(salt).decode(),
            base64.b64encode(digest).decode(),
        )
    )


def verify_password(password: str, stored: str | None) -> bool:
    try:
        scheme, n, r, p, salt, expected = str(stored).split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    try:
        expected_bytes = base64.b64decode(expected)
        actual = hashlib.scrypt(
            unicodedata.normalize("NFKC", str(password)).encode(),
            salt=base64.b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected_bytes),
            maxmem=_maxmem(int(n), int(r)),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected_bytes)


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    return hash_password(generate_password())


def dummy_verify(password: str) -> None:
    """Tarda lo mismo que una verificación real: no revela qué usuarios existen."""
    verify_password(password, _dummy_hash())
