"""Usuarios, sesiones, claves de API e inicio de sesión OIDC (SQLAlchemy ORM)."""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from typing import Any

from sqlalchemy import Executable, delete, func, insert, literal_column, or_, select, update

from invoicebot.infrastructure.db.connection import Database
from invoicebot.infrastructure.db.invoice_repository import as_row
from invoicebot.infrastructure.db.models import (
    USER_COMPANIES,
    ApiKey,
    OidcRequest,
    ReviewSession,
    User,
    utcnow,
)

Row = dict[str, Any]

_PUBLIC_COLUMNS = (
    User.id,
    User.username,
    User.display_name,
    User.active,
    User.created_at,
    User.updated_at,
    User.last_login_at,
    User.role,
    User.email,
    User.auth_provider,
    User.all_companies,
)
_KEY_COLUMNS = (
    ApiKey.id,
    ApiKey.name,
    ApiKey.prefix,
    ApiKey.scope,
    ApiKey.company_id,
    ApiKey.permissions,
    ApiKey.created_by,
    ApiKey.active,
    ApiKey.created_at,
    ApiKey.last_used_at,
    ApiKey.revoked_at,
)


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def _email(value: str | None) -> str | None:
    return (value or "").strip().lower()[:254] or None


def _minutes_from_now(minutes: int) -> Any:
    """Hora del servidor de base de datos (no la del contenedor) más unos minutos."""
    return func.dateadd(literal_column("MINUTE"), minutes, func.sysutcdatetime())


class UserRepository:
    def __init__(self, db: Database):
        self.db = db

    def _one(self, query: Executable) -> Row | None:
        with self.db.begin() as conn:
            row = conn.execute(query).mappings().first()
        return as_row(row) if row is not None else None

    def _all(self, query: Executable) -> list[Row]:
        with self.db.begin() as conn:
            return [as_row(r) for r in conn.execute(query).mappings()]

    # --- Usuarios ---------------------------------------------------------------

    def find_by_username(self, username: str) -> Row | None:
        return self._one(select(User).where(User.username == username))

    def find_by_email(self, email: str) -> Row | None:
        return self._one(select(User).where(User.email == email.strip().lower()))

    def find_by_subject(self, subject: str) -> Row | None:
        return self._one(select(User).where(User.external_subject == subject))

    def get_by_id(self, user_id: int) -> Row | None:
        return self._one(select(*_PUBLIC_COLUMNS).where(User.id == user_id))

    def list_users(self) -> list[Row]:
        return self._all(select(*_PUBLIC_COLUMNS).order_by(User.username))

    def count_active(self) -> int:
        with self.db.begin() as conn:
            query = select(func.count()).select_from(User).where(User.active == True)  # noqa: E712
            return int(conn.execute(query).scalar_one())

    def insert(
        self,
        *,
        username: str,
        display_name: str,
        password_hash: str,
        role: str = "admin",
        email: str | None = None,
        auth_provider: str = "local",
        external_subject: str | None = None,
    ) -> int:
        with self.db.begin() as conn:
            return int(
                conn.execute(
                    insert(User)
                    .values(
                        username=username,
                        display_name=display_name[:100],
                        password_hash=password_hash,
                        role=role,
                        email=_email(email),
                        auth_provider=auth_provider,
                        external_subject=external_subject,
                    )
                    .returning(User.id)
                ).scalar_one()
            )

    def set_password(self, user_id: int, password_hash: str) -> None:
        with self.db.begin() as conn:
            conn.execute(
                update(User)
                .where(User.id == user_id)
                .values(password_hash=password_hash, updated_at=func.sysutcdatetime())
            )
            # Cambiar la contraseña cierra todas las sesiones abiertas.
            conn.execute(delete(ReviewSession).where(ReviewSession.user_id == user_id))

    def set_active(self, user_id: int, active: bool) -> None:
        with self.db.begin() as conn:
            conn.execute(
                update(User)
                .where(User.id == user_id)
                .values(active=active, updated_at=func.sysutcdatetime())
            )
            if not active:
                conn.execute(delete(ReviewSession).where(ReviewSession.user_id == user_id))

    def touch_login(self, user_id: int) -> None:
        with self.db.begin() as conn:
            conn.execute(
                update(User).where(User.id == user_id).values(last_login_at=func.sysutcdatetime())
            )

    # --- Roles, empresas e identidad externa ---------------------------------------

    def set_role(self, user_id: int, role: str) -> None:
        with self.db.begin() as conn:
            conn.execute(update(User).where(User.id == user_id).values(role=role))

    def set_email(self, user_id: int, email: str | None) -> None:
        with self.db.begin() as conn:
            conn.execute(update(User).where(User.id == user_id).values(email=_email(email)))

    def set_companies(self, user_id: int, company_ids: list[int] | None) -> None:
        """None: todas las empresas · lista: solo esas (vacía = ninguna)."""
        with self.db.begin() as conn:
            conn.execute(delete(USER_COMPANIES).where(USER_COMPANIES.c.user_id == user_id))
            if company_ids:
                conn.execute(
                    insert(USER_COMPANIES),
                    [{"user_id": user_id, "company_id": c} for c in company_ids],
                )
            conn.execute(
                update(User).where(User.id == user_id).values(all_companies=company_ids is None)
            )

    def company_ids(self, user_id: int) -> list[int]:
        with self.db.begin() as conn:
            query = (
                select(USER_COMPANIES.c.company_id)
                .where(USER_COMPANIES.c.user_id == user_id)
                .order_by(USER_COMPANIES.c.company_id)
            )
            return [int(c) for c in conn.execute(query).scalars()]

    def link_external(self, user_id: int, *, subject: str, provider: str = "oidc") -> None:
        with self.db.begin() as conn:
            conn.execute(
                update(User)
                .where(User.id == user_id)
                .values(external_subject=subject[:200], auth_provider=provider)
            )

    # --- Inicio de sesión OIDC (estado entre la redirección y la vuelta) -------------

    def save_oidc_request(
        self, *, state: str, nonce: str, code_verifier: str, redirect_uri: str
    ) -> None:
        with self.db.begin() as conn:
            conn.execute(
                delete(OidcRequest).where(OidcRequest.created_at < utcnow() - timedelta(minutes=15))
            )
            conn.execute(
                insert(OidcRequest).values(
                    state=state,
                    nonce=nonce,
                    code_verifier=code_verifier,
                    redirect_uri=redirect_uri[:500],
                )
            )

    def pop_oidc_request(self, state: str) -> Row | None:
        """La solicitud (de un solo uso, válida 10 minutos) o None."""
        if not state or len(state) > 64:
            return None
        with self.db.begin() as conn:
            row = (
                conn.execute(
                    delete(OidcRequest)
                    .where(
                        OidcRequest.state == state,
                        OidcRequest.created_at > utcnow() - timedelta(minutes=10),
                    )
                    .returning(OidcRequest)
                )
                .mappings()
                .first()
            )
        return as_row(row) if row is not None else None

    # --- Sesiones ---------------------------------------------------------------

    def create_session(self, user_id: int, *, hours: int, ip: str | None) -> tuple[str, str]:
        """Devuelve (identificador de sesión, token antifalsificación).

        Se guarda el hash del identificador: quien lea la tabla no puede suplantar
        a nadie. `user_version` (users.updated_at al entrar) invalida la sesión si
        cambia la contraseña o se desactiva al usuario.
        """
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_hex(24)
        with self.db.begin() as conn:
            conn.execute(
                delete(ReviewSession).where(ReviewSession.expires_at < func.sysutcdatetime())
            )
            version = conn.execute(select(User.updated_at).where(User.id == user_id)).scalar()
            if version is not None:
                conn.execute(
                    insert(ReviewSession).values(
                        token_hash=hash_token(token),
                        user_id=user_id,
                        user_version=version,
                        csrf_token=csrf,
                        ip=(ip or "")[:64],
                        expires_at=_minutes_from_now(hours * 60),
                    )
                )
        return token, csrf

    def get_session(self, token: str | None) -> Row | None:
        if not token or len(token) > 100:
            return None
        return self._one(
            select(
                ReviewSession.user_id,
                ReviewSession.csrf_token,
                ReviewSession.expires_at,
                User.username,
                User.display_name,
                User.role,
                User.all_companies,
            )
            .join(User, User.id == ReviewSession.user_id)
            .where(
                ReviewSession.token_hash == hash_token(token),
                ReviewSession.expires_at > func.sysutcdatetime(),
                User.active == True,  # noqa: E712
                User.updated_at == ReviewSession.user_version,
            )
        )

    def delete_session(self, token: str | None) -> None:
        if token:
            with self.db.begin() as conn:
                conn.execute(
                    delete(ReviewSession).where(ReviewSession.token_hash == hash_token(token))
                )

    # --- Claves de API ---------------------------------------------------------

    def create_api_key(
        self,
        *,
        name: str,
        scope: str,
        created_by: str | None,
        company_id: int | None = None,
        permissions: str | None = None,
    ) -> tuple[str, Row]:
        """Crea una clave `ibk_<prefijo>_<secreto>`; se devuelve una sola vez."""
        prefix = secrets.token_hex(4)
        key = f"ibk_{prefix}_{secrets.token_urlsafe(32)}"
        with self.db.begin() as conn:
            row = (
                conn.execute(
                    insert(ApiKey)
                    .values(
                        name=name[:100],
                        prefix=f"ibk_{prefix}",
                        key_hash=hash_token(key),
                        scope=scope,
                        created_by=(created_by or "")[:100] or None,
                        company_id=company_id,
                        permissions=permissions,
                    )
                    .returning(
                        ApiKey.id,
                        ApiKey.name,
                        ApiKey.prefix,
                        ApiKey.scope,
                        ApiKey.created_by,
                        ApiKey.active,
                        ApiKey.created_at,
                        ApiKey.company_id,
                        ApiKey.permissions,
                    )
                )
                .mappings()
                .one()
            )
        return key, as_row(row)

    def find_api_key(self, key: str | None) -> Row | None:
        if not key or len(key) > 200 or not key.startswith("ibk_"):
            return None
        row = self._one(
            select(
                ApiKey.id,
                ApiKey.name,
                ApiKey.prefix,
                ApiKey.scope,
                ApiKey.company_id,
                ApiKey.permissions,
            ).where(ApiKey.key_hash == hash_token(key), ApiKey.active == True)  # noqa: E712
        )
        if row:
            with self.db.begin() as conn:
                conn.execute(
                    update(ApiKey)
                    .where(
                        ApiKey.id == row["id"],
                        or_(
                            ApiKey.last_used_at.is_(None),
                            ApiKey.last_used_at < _minutes_from_now(-1),
                        ),
                    )
                    .values(last_used_at=func.sysutcdatetime())
                )
        return row

    def list_api_keys(self) -> list[Row]:
        return self._all(select(*_KEY_COLUMNS).order_by(ApiKey.id))

    def revoke_api_key(self, prefix_or_id: str) -> int:
        condition = (
            ApiKey.id == int(prefix_or_id)
            if prefix_or_id.isdigit()
            else ApiKey.prefix == prefix_or_id
        )
        with self.db.begin() as conn:
            result = conn.execute(
                update(ApiKey)
                .where(condition, ApiKey.active == True)  # noqa: E712
                .values(active=False, revoked_at=func.sysutcdatetime())
            )
            return int(result.rowcount or 0)
