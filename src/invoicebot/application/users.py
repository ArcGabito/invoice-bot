"""Usuarios de la pantalla de revisión y claves de API."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from invoicebot.domain.access import ROLES, AccessError, parse_permissions
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


def _check_role(role: str) -> str:
    if role not in ROLES:
        raise UserError(f"Rol desconocido {role!r} (usa {', '.join(ROLES)})")
    return role


def add_user(
    users: UserRepository,
    username: str,
    *,
    display_name: str | None = None,
    password: str | None = None,
    role: str = "admin",
    email: str | None = None,
    company_ids: list[int] | None = None,
) -> UserWithPassword:
    """Crea un usuario. ``company_ids`` None = todas las empresas."""
    name = normalize_username(username)
    if users.find_by_username(name):
        raise UserError(f'El usuario "{name}" ya existe')
    if email and users.find_by_email(email):
        raise UserError(f"Ya hay un usuario con el correo {email}")
    generated = password is None
    secret = generate_password() if generated else _check_password(password)
    user_id = users.insert(
        username=name,
        display_name=(display_name or "").strip() or name,
        password_hash=hash_password(secret),
        role=_check_role(role),
        email=email,
    )
    if company_ids is not None:
        users.set_companies(user_id, company_ids)
    user = users.get_by_id(user_id)
    assert user is not None
    return UserWithPassword(user, secret, generated)


def set_role(users: UserRepository, username: str, role: str) -> dict[str, Any]:
    user = _require(users, username)
    users.set_role(user["id"], _check_role(role))
    public = users.get_by_id(user["id"])
    assert public is not None
    return public


def set_companies(
    users: UserRepository, username: str, company_ids: list[int] | None
) -> dict[str, Any]:
    user = _require(users, username)
    users.set_companies(user["id"], company_ids)
    public = users.get_by_id(user["id"])
    assert public is not None
    public["company_ids"] = users.company_ids(user["id"])
    return public


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
    users: UserRepository,
    name: str,
    *,
    scope: str,
    created_by: str | None,
    company_id: int | None = None,
    permissions: str | None = None,
) -> tuple[str, dict[str, Any]]:
    label = (name or "").strip()
    if not label:
        raise UserError("Indica un nombre para la clave (p. ej. el sistema que la usará)")
    if scope not in ("read", "write"):
        raise UserError("El alcance de la clave debe ser read o write")
    normalized = None
    if permissions:
        try:
            normalized = ",".join(sorted(parse_permissions(permissions)))
        except AccessError as error:
            raise UserError(str(error)) from error
    return users.create_api_key(
        name=label,
        scope=scope,
        created_by=created_by,
        company_id=company_id,
        permissions=normalized,
    )


def _username_from_email(users: UserRepository, email: str) -> str:
    base = re.sub(r"[^a-z0-9._-]", "", email.split("@", maxsplit=1)[0].lower())[:36] or "usuario"
    if len(base) < 2:
        base = f"{base}00"
    candidate, n = base, 2
    while users.find_by_username(candidate):
        candidate = f"{base[:36]}{n}"
        n += 1
    return candidate


def resolve_oidc_user(
    users: UserRepository,
    *,
    subject: str,
    email: str | None,
    name: str | None,
    email_verified: bool,
    allowed_domains: tuple[str, ...],
    auto_create_role: str | None,
) -> dict[str, Any] | None:
    """Usuario para una identidad del proveedor, o None si no tiene acceso.

    1. Por el identificador del proveedor (`sub`), si ya entró antes.
    2. Por el correo verificado de un usuario creado por un administrador: se vincula.
    3. Si INVOICEBOT_OIDC_AUTO_CREATE_ROLE está definido, se crea con ese rol (nunca admin).
    """
    domain = (email or "").rsplit("@", 1)[-1]
    if allowed_domains and (not email or domain not in allowed_domains):
        return None
    user = users.find_by_subject(subject)
    if user is None and email and email_verified:
        user = users.find_by_email(email)
        if user is not None:
            users.link_external(user["id"], subject=subject)
    if user is None and auto_create_role and email and email_verified:
        user_id = users.insert(
            username=_username_from_email(users, email),
            display_name=(name or email)[:100],
            # Sin contraseña local: solo entra por el proveedor.
            password_hash="!",
            role=auto_create_role,
            email=email,
            auth_provider="oidc",
            external_subject=subject,
        )
        user = users.find_by_username(users.get_by_id(user_id)["username"])  # type: ignore[index]
    if user is None or not user["active"]:
        return None
    users.touch_login(user["id"])
    return users.get_by_id(user["id"])
