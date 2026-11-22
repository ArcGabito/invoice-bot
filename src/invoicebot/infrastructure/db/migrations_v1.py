"""Migraciones de la versión 1.x (Python / FastAPI)."""

from __future__ import annotations

from invoicebot.infrastructure.db.migration import Migration

#: Claves de API para integrar otros sistemas (ERP, RPA, email-bot) con la API REST,
#: y ciclos del bot lanzados desde la API.
M005 = Migration(
    5,
    "005_api_keys",
    (
        """CREATE TABLE api_keys (
      id           INT IDENTITY(1, 1) NOT NULL CONSTRAINT pk_api_keys PRIMARY KEY,
      name         NVARCHAR(100) NOT NULL,
      -- Prefijo visible (ibk_xxxxxxxx) para reconocer la clave sin revelarla.
      prefix       NVARCHAR(16)  NOT NULL,
      -- SHA-256 de la clave completa: la clave nunca se guarda.
      key_hash     BINARY(32)    NOT NULL CONSTRAINT uq_api_keys_hash UNIQUE,
      -- read: consultas y exportaciones · write: además subir documentos, revisar y lanzar ciclos.
      scope        NVARCHAR(10)  NOT NULL CONSTRAINT ck_api_keys_scope CHECK (scope IN (N'read', N'write')),
      created_by   NVARCHAR(100) NULL,
      active       BIT           NOT NULL CONSTRAINT df_api_keys_active DEFAULT 1,
      created_at   DATETIME2(3)  NOT NULL CONSTRAINT df_api_keys_created DEFAULT SYSUTCDATETIME(),
      last_used_at DATETIME2(3)  NULL,
      revoked_at   DATETIME2(3)  NULL
    )""",
        """ALTER TABLE bot_runs DROP CONSTRAINT ck_bot_runs_trigger""",
        """ALTER TABLE bot_runs ADD CONSTRAINT ck_bot_runs_trigger
      CHECK ([trigger] IN (N'manual', N'scheduled', N'watch', N'api'))""",
    ),
)

V1_MIGRATIONS = (M005,)
