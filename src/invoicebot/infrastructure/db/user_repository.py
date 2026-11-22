"""Usuarios de la pantalla de revisión, sus sesiones y las claves de API."""

from __future__ import annotations

import hashlib
import secrets
from typing import Any

from sqlalchemy import text

from invoicebot.infrastructure.db.connection import Database
from invoicebot.infrastructure.db.invoice_repository import as_row

Row = dict[str, Any]

_PUBLIC_COLUMNS = (
    "id, username, display_name, active, created_at, updated_at, last_login_at, role, email, "
    "auth_provider, all_companies"
)


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


class UserRepository:
    def __init__(self, db: Database):
        self.db = db

    def _one(self, sql: str, params: dict[str, Any] | None = None) -> Row | None:
        with self.db.begin() as conn:
            row = conn.execute(text(sql), params or {}).first()
        return as_row(row._mapping) if row is not None else None

    def _all(self, sql: str, params: dict[str, Any] | None = None) -> list[Row]:
        with self.db.begin() as conn:
            return [as_row(r._mapping) for r in conn.execute(text(sql), params or {})]

    def _exec(self, sql: str, params: dict[str, Any] | None = None) -> int:
        with self.db.begin() as conn:
            return int(conn.execute(text(sql), params or {}).rowcount or 0)

    # --- Usuarios ---------------------------------------------------------------

    def find_by_username(self, username: str) -> Row | None:
        return self._one("SELECT * FROM users WHERE username = :u", {"u": username})

    def get_by_id(self, user_id: int) -> Row | None:
        return self._one(f"SELECT {_PUBLIC_COLUMNS} FROM users WHERE id = :id", {"id": user_id})

    def list_users(self) -> list[Row]:
        return self._all(f"SELECT {_PUBLIC_COLUMNS} FROM users ORDER BY username")

    def count_active(self) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM users WHERE active = 1")
        return int((row or {}).get("n") or 0)

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
        row = self._one(
            """INSERT INTO users (username, display_name, password_hash, role, email,
                 auth_provider, external_subject) OUTPUT inserted.id
               VALUES (:u, :d, :p, :r, :e, :a, :s)""",
            {
                "u": username,
                "d": display_name[:100],
                "p": password_hash,
                "r": role,
                "e": (email or "").strip().lower()[:254] or None,
                "a": auth_provider,
                "s": external_subject,
            },
        )
        assert row is not None
        return int(row["id"])

    def set_password(self, user_id: int, password_hash: str) -> None:
        with self.db.begin() as conn:
            conn.execute(
                text(
                    "UPDATE users SET password_hash = :p, updated_at = SYSUTCDATETIME() WHERE id = :id"
                ),
                {"p": password_hash, "id": user_id},
            )
            # Cambiar la contraseña cierra todas las sesiones abiertas.
            conn.execute(text("DELETE FROM review_sessions WHERE user_id = :id"), {"id": user_id})

    def set_active(self, user_id: int, active: bool) -> None:
        with self.db.begin() as conn:
            conn.execute(
                text("UPDATE users SET active = :a, updated_at = SYSUTCDATETIME() WHERE id = :id"),
                {"a": 1 if active else 0, "id": user_id},
            )
            if not active:
                conn.execute(
                    text("DELETE FROM review_sessions WHERE user_id = :id"), {"id": user_id}
                )

    def touch_login(self, user_id: int) -> None:
        self._exec(
            "UPDATE users SET last_login_at = SYSUTCDATETIME() WHERE id = :id", {"id": user_id}
        )

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
            conn.execute(text("DELETE FROM review_sessions WHERE expires_at < SYSUTCDATETIME()"))
            conn.execute(
                text(
                    """INSERT INTO review_sessions
                         (token_hash, user_id, user_version, csrf_token, ip, expires_at)
                       SELECT :h, id, updated_at, :c, :ip, DATEADD(MINUTE, :m, SYSUTCDATETIME())
                       FROM users WHERE id = :u"""
                ),
                {
                    "h": hash_token(token),
                    "c": csrf,
                    "ip": (ip or "")[:64],
                    "m": hours * 60,
                    "u": user_id,
                },
            )
        return token, csrf

    def get_session(self, token: str | None) -> Row | None:
        if not token or len(token) > 100:
            return None
        return self._one(
            """SELECT s.user_id, s.csrf_token, s.expires_at, u.username, u.display_name, u.role,
                      u.all_companies
               FROM review_sessions s JOIN users u ON u.id = s.user_id
               WHERE s.token_hash = :h AND s.expires_at > SYSUTCDATETIME() AND u.active = 1
                 AND u.updated_at = s.user_version""",
            {"h": hash_token(token)},
        )

    def delete_session(self, token: str | None) -> None:
        if token:
            self._exec(
                "DELETE FROM review_sessions WHERE token_hash = :h", {"h": hash_token(token)}
            )

    # --- Roles, empresas e identidad externa ---------------------------------------

    def set_role(self, user_id: int, role: str) -> None:
        self._exec("UPDATE users SET role = :r WHERE id = :id", {"r": role, "id": user_id})

    def set_email(self, user_id: int, email: str | None) -> None:
        self._exec(
            "UPDATE users SET email = :e WHERE id = :id",
            {"e": (email or "").strip().lower()[:254] or None, "id": user_id},
        )

    def set_companies(self, user_id: int, company_ids: list[int] | None) -> None:
        """None: todas las empresas · lista: solo esas (vacía = ninguna)."""
        with self.db.begin() as conn:
            conn.execute(text("DELETE FROM user_companies WHERE user_id = :u"), {"u": user_id})
            for company_id in company_ids or []:
                conn.execute(
                    text("INSERT INTO user_companies (user_id, company_id) VALUES (:u, :c)"),
                    {"u": user_id, "c": company_id},
                )
            conn.execute(
                text("UPDATE users SET all_companies = :a WHERE id = :u"),
                {"a": 1 if company_ids is None else 0, "u": user_id},
            )

    def company_ids(self, user_id: int) -> list[int]:
        return [
            int(r["company_id"])
            for r in self._all(
                "SELECT company_id FROM user_companies WHERE user_id = :u ORDER BY company_id",
                {"u": user_id},
            )
        ]

    def find_by_email(self, email: str) -> Row | None:
        return self._one("SELECT * FROM users WHERE email = :e", {"e": email.strip().lower()})

    def find_by_subject(self, subject: str) -> Row | None:
        return self._one("SELECT * FROM users WHERE external_subject = :s", {"s": subject})

    def link_external(self, user_id: int, *, subject: str, provider: str = "oidc") -> None:
        self._exec(
            "UPDATE users SET external_subject = :s, auth_provider = :p WHERE id = :id",
            {"s": subject[:200], "p": provider, "id": user_id},
        )

    # --- Inicio de sesión OIDC (estado entre la redirección y la vuelta) -------------

    def save_oidc_request(
        self, *, state: str, nonce: str, code_verifier: str, redirect_uri: str
    ) -> None:
        with self.db.begin() as conn:
            conn.execute(
                text(
                    "DELETE FROM oidc_requests WHERE created_at < DATEADD(MINUTE, -15, SYSUTCDATETIME())"
                )
            )
            conn.execute(
                text(
                    """INSERT INTO oidc_requests (state, nonce, code_verifier, redirect_uri)
                       VALUES (:s, :n, :v, :r)"""
                ),
                {"s": state, "n": nonce, "v": code_verifier, "r": redirect_uri[:500]},
            )

    def pop_oidc_request(self, state: str) -> Row | None:
        """La solicitud (de un solo uso, válida 10 minutos) o None."""
        if not state or len(state) > 64:
            return None
        return self._one(
            """DELETE FROM oidc_requests OUTPUT deleted.*
               WHERE state = :s AND created_at > DATEADD(MINUTE, -10, SYSUTCDATETIME())""",
            {"s": state},
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
        row = self._one(
            """INSERT INTO api_keys (name, prefix, key_hash, scope, created_by, company_id,
                 permissions)
               OUTPUT inserted.id, inserted.name, inserted.prefix, inserted.scope,
                      inserted.created_by, inserted.active, inserted.created_at,
                      inserted.company_id, inserted.permissions
               VALUES (:n, :p, :h, :s, :c, :company, :perm)""",
            {
                "n": name[:100],
                "p": f"ibk_{prefix}",
                "h": hash_token(key),
                "s": scope,
                "c": (created_by or "")[:100] or None,
                "company": company_id,
                "perm": permissions,
            },
        )
        assert row is not None
        return key, row

    def find_api_key(self, key: str | None) -> Row | None:
        if not key or len(key) > 200 or not key.startswith("ibk_"):
            return None
        row = self._one(
            """SELECT id, name, prefix, scope, company_id, permissions FROM api_keys
               WHERE key_hash = :h AND active = 1""",
            {"h": hash_token(key)},
        )
        if row:
            self._exec(
                "UPDATE api_keys SET last_used_at = SYSUTCDATETIME() WHERE id = :id "
                "AND (last_used_at IS NULL OR last_used_at < DATEADD(MINUTE, -1, SYSUTCDATETIME()))",
                {"id": row["id"]},
            )
        return row

    def list_api_keys(self) -> list[Row]:
        return self._all(
            """SELECT id, name, prefix, scope, company_id, permissions, created_by, active,
                      created_at, last_used_at, revoked_at FROM api_keys ORDER BY id"""
        )

    def revoke_api_key(self, prefix_or_id: str) -> int:
        params: dict[str, Any] = {"p": prefix_or_id}
        condition = "prefix = :p"
        if prefix_or_id.isdigit():
            condition = "id = :id"
            params = {"id": int(prefix_or_id)}
        return self._exec(
            f"UPDATE api_keys SET active = 0, revoked_at = SYSUTCDATETIME() "
            f"WHERE {condition} AND active = 1",
            params,
        )
