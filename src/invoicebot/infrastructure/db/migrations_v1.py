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

#: Trabajos en cola (Dramatiq + Redis o en el mismo proceso) y tareas programadas.
#: SQL Server es la fuente de verdad: Redis solo transporta el id del trabajo.
M006 = Migration(
    6,
    "006_jobs_and_schedules",
    (
        """CREATE TABLE jobs (
      id              BIGINT IDENTITY(1, 1) NOT NULL CONSTRAINT pk_jobs PRIMARY KEY,
      task            NVARCHAR(100)  NOT NULL,
      queue           NVARCHAR(40)   NOT NULL,
      status          NVARCHAR(12)   NOT NULL CONSTRAINT ck_jobs_status
        CHECK (status IN (N'queued', N'running', N'retrying', N'succeeded', N'failed', N'cancelled')),
      payload         NVARCHAR(MAX)  NULL,
      result          NVARCHAR(MAX)  NULL,
      error_message   NVARCHAR(4000) NULL,
      attempts        INT            NOT NULL CONSTRAINT df_jobs_attempts DEFAULT 0,
      max_attempts    INT            NOT NULL,
      -- Mientras el trabajo está activo: impide encolar dos veces lo mismo
      -- (p. ej. documents.process:123). Se borra al terminar.
      dedupe_key      NVARCHAR(200)  NULL,
      -- Cabecera Idempotency-Key de la API: repetir la petición devuelve el mismo trabajo.
      idempotency_key NVARCHAR(200)  NULL,
      parent_id       BIGINT         NULL,
      created_by      NVARCHAR(150)  NULL,
      worker          NVARCHAR(200)  NULL,
      run_after       DATETIME2(3)   NULL,
      -- NULL: falta enviarlo a la cola (lo hace quien lo crea o, si falla, el programador).
      published_at    DATETIME2(3)   NULL,
      created_at      DATETIME2(3)   NOT NULL CONSTRAINT df_jobs_created DEFAULT SYSUTCDATETIME(),
      started_at      DATETIME2(3)   NULL,
      finished_at     DATETIME2(3)   NULL,
      updated_at      DATETIME2(3)   NOT NULL CONSTRAINT df_jobs_updated DEFAULT SYSUTCDATETIME()
    )""",
        "CREATE UNIQUE INDEX uq_jobs_dedupe ON jobs (dedupe_key) WHERE dedupe_key IS NOT NULL",
        "CREATE UNIQUE INDEX uq_jobs_idempotency ON jobs (idempotency_key) WHERE idempotency_key IS NOT NULL",
        "CREATE INDEX ix_jobs_status ON jobs (status, published_at) INCLUDE (run_after, created_at)",
        "CREATE INDEX ix_jobs_task ON jobs (task, id DESC)",
        "CREATE INDEX ix_jobs_parent ON jobs (parent_id) WHERE parent_id IS NOT NULL",
        """CREATE TABLE schedules (
      name        NVARCHAR(60)  NOT NULL CONSTRAINT pk_schedules PRIMARY KEY,
      task        NVARCHAR(100) NOT NULL,
      cron        NVARCHAR(100) NOT NULL,
      payload     NVARCHAR(MAX) NULL,
      enabled     BIT           NOT NULL CONSTRAINT df_schedules_enabled DEFAULT 1,
      -- config: viene de INVOICEBOT_SCHEDULE_* y se actualiza sola · manual: creada por una persona.
      source      NVARCHAR(10)  NOT NULL CONSTRAINT df_schedules_source DEFAULT N'config'
        CONSTRAINT ck_schedules_source CHECK (source IN (N'config', N'manual')),
      next_run_at DATETIME2(3)  NULL,
      last_run_at DATETIME2(3)  NULL,
      last_job_id BIGINT        NULL,
      updated_at  DATETIME2(3)  NOT NULL CONSTRAINT df_schedules_updated DEFAULT SYSUTCDATETIME()
    )""",
        """ALTER TABLE bot_runs DROP CONSTRAINT ck_bot_runs_trigger""",
        """ALTER TABLE bot_runs ADD CONSTRAINT ck_bot_runs_trigger
      CHECK ([trigger] IN (N'manual', N'scheduled', N'watch', N'api', N'queue'))""",
    ),
)

V1_MIGRATIONS = (M005, M006)
