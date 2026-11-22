"""Usuarios de la pantalla de revisión y claves de API."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from invoicebot.errors import UserError
from invoicebot.infrastructure.db.user_repository import UserRepository
from invoicebot.infrastructure.security import (
    dummy_verify,
    generate_password,
    hash_password,
    verify_password,
)

MIN_PASSWORD_LENGTH = 12
_MAX_PASSWORD_LENGTH = 256
_USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{1,39}$")


def normalize_username(username: object) -> str:
    """En minúsculas: "Ana" y "ana" son la misma persona."""
    name = str(username or "").strip().lower()
    if not _USERNAME.match(name):
        raise UserError(
            "El usuario debe tener de 2 a 40 caracteres: letras sin tildes, números, punto, guion o guion bajo"
        )
    return name


def _check_password(password: object) -> str:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise UserError(f"La contraseña debe tener al menos {MIN_PASSWORD_LENGTH} caracteres")
    if len(password) > _MAX_PASSWORD_LENGTH:
        raise UserError("La contraseña es demasiado larga")
    return password


def _require(users: UserRepository, username: str) -> dict[str, Any]:
    user = users.find_by_username(normalize_username(username))
    if not user:
        raise UserError(f'No existe el usuario "{username}"')
    return user


@dataclass
class UserWithPassword:
    user: dict[str, Any]
    #: Se muestra una sola vez para entregársela a la persona.
    password: str
    generated: bool


def add_user(
    users: UserRepository,
    username: str,
    *,
    display_name: str | None = None,
    password: str | None = None,
) -> UserWithPassword:
    name = normalize_username(username)
    if users.find_by_username(name):
        raise UserError(f'El usuario "{name}" ya existe')
    generated = password is None
    secret = generate_password() if generated else _check_password(password)
    user_id = users.insert(
        username=name,
        display_name=(display_name or "").strip() or name,
        password_hash=hash_password(secret),
    )
    user = users.get_by_id(user_id)
    assert user is not None
    return UserWithPassword(user, secret, generated)


def reset_password(
    users: UserRepository, username: str, *, password: str | None = None
) -> UserWithPassword:
    user = _require(users, username)
    generated = password is None
    secret = generate_password() if generated else _check_password(password)
    users.set_password(user["id"], hash_password(secret))
    public = users.get_by_id(user["id"])
    assert public is not None
    return UserWithPassword(public, secret, generated)


def set_user_active(users: UserRepository, username: str, *, active: bool) -> dict[str, Any]:
    user = _require(users, username)
    users.set_active(user["id"], active)
    public = users.get_by_id(user["id"])
    assert public is not None
    return public


def authenticate(
    users: UserRepository, username: object, password: object
) -> dict[str, Any] | None:
    """Usuario (sin hash) o None; tarda lo mismo si el usuario no existe."""
    try:
        name = normalize_username(username)
    except UserError:
        dummy_verify(str(password or ""))
        return None
    user = users.find_by_username(name)
    if not user or not isinstance(password, str) or len(password) > _MAX_PASSWORD_LENGTH:
        dummy_verify(str(password or ""))
        return None
    if not verify_password(password, user["password_hash"]) or not user["active"]:
        return None
    users.touch_login(user["id"])
    return users.get_by_id(user["id"])


def create_api_key(
    users: UserRepository, name: str, *, scope: str, created_by: str | None
) -> tuple[str, dict[str, Any]]:
    label = (name or "").strip()
    if not label:
        raise UserError("Indica un nombre para la clave (p. ej. el sistema que la usará)")
    if scope not in ("read", "write"):
        raise UserError("El alcance de la clave debe ser read o write")
    return users.create_api_key(name=label, scope=scope, created_by=created_by)
