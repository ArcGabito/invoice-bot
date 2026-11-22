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

#: Multiempresa, roles, inicio de sesión con Microsoft/Google (OIDC), auditoría con triggers
#: y Row-Level Security: una persona o clave limitada a ciertas empresas no ve
#: (ni por la API ni por SQL) los comprobantes de las demás.
M007 = Migration(
    7,
    "007_companies_roles_audit",
    (
        """CREATE TABLE companies (
      id                 INT IDENTITY(1, 1) NOT NULL CONSTRAINT pk_companies PRIMARY KEY,
      ruc                NVARCHAR(11)  NOT NULL CONSTRAINT uq_companies_ruc UNIQUE,
      name               NVARCHAR(200) NOT NULL,
      -- Agente de retención del IGV (3 %) y buen contribuyente (exceptúa de retención).
      retention_agent    BIT           NOT NULL CONSTRAINT df_companies_retention DEFAULT 0,
      good_taxpayer      BIT           NOT NULL CONSTRAINT df_companies_good DEFAULT 0,
      active             BIT           NOT NULL CONSTRAINT df_companies_active DEFAULT 1,
      created_at         DATETIME2(3)  NOT NULL CONSTRAINT df_companies_created DEFAULT SYSUTCDATETIME()
    )""",
        """ALTER TABLE invoices ADD company_id INT NULL
      CONSTRAINT fk_invoices_company REFERENCES companies (id)""",
        "CREATE INDEX ix_invoices_company ON invoices (company_id, issue_date)",
        # Los usuarios que ya existían conservan todos los permisos (admin).
        """ALTER TABLE users ADD
      role             NVARCHAR(20)  NOT NULL CONSTRAINT df_users_role DEFAULT N'admin'
        CONSTRAINT ck_users_role CHECK (role IN (N'admin', N'accountant', N'approver', N'treasury', N'auditor')),
      email            NVARCHAR(254) NULL,
      -- local: contraseña · oidc: Microsoft Entra ID / Google (sin contraseña local).
      auth_provider    NVARCHAR(20)  NOT NULL CONSTRAINT df_users_provider DEFAULT N'local',
      external_subject NVARCHAR(200) NULL,
      -- 1: ve todas las empresas · 0: solo las de user_companies.
      all_companies    BIT           NOT NULL CONSTRAINT df_users_all_companies DEFAULT 1""",
        "CREATE UNIQUE INDEX uq_users_email ON users (email) WHERE email IS NOT NULL",
        "CREATE UNIQUE INDEX uq_users_subject ON users (external_subject) WHERE external_subject IS NOT NULL",
        """CREATE TABLE user_companies (
      user_id    INT NOT NULL CONSTRAINT fk_user_companies_user REFERENCES users (id) ON DELETE CASCADE,
      company_id INT NOT NULL CONSTRAINT fk_user_companies_company REFERENCES companies (id),
      CONSTRAINT pk_user_companies PRIMARY KEY (user_id, company_id)
    )""",
        """ALTER TABLE api_keys ADD
      company_id  INT NULL CONSTRAINT fk_api_keys_company REFERENCES companies (id),
      -- Permisos finos separados por comas; NULL = los del alcance (read/write).
      permissions NVARCHAR(400) NULL""",
        """CREATE TABLE oidc_requests (
      state         NVARCHAR(64)  NOT NULL CONSTRAINT pk_oidc_requests PRIMARY KEY,
      nonce         NVARCHAR(64)  NOT NULL,
      code_verifier NVARCHAR(128) NOT NULL,
      redirect_uri  NVARCHAR(500) NOT NULL,
      created_at    DATETIME2(3)  NOT NULL CONSTRAINT df_oidc_requests_created DEFAULT SYSUTCDATETIME()
    )""",
        # Auditoría de facturas con un trigger: cada alta, cambio o baja deja el estado
        # resultante y quién lo hizo (SESSION_CONTEXT('invoicebot.actor'): la persona o clave
        # de la API, o "job:<tarea>#<id>" en los workers). No se usa una tabla temporal: con
        # READ_COMMITTED_SNAPSHOT y escrituras concurrentes sobre la misma factura (worker y
        # revisor) SQL Server rechazaría la segunda (error 13535).
        """CREATE TABLE invoice_changes (
      id                BIGINT IDENTITY(1, 1) NOT NULL CONSTRAINT pk_invoice_changes PRIMARY KEY,
      invoice_id        INT           NOT NULL,
      operation         NCHAR(1)      NOT NULL CONSTRAINT ck_invoice_changes_op CHECK (operation IN (N'I', N'U', N'D')),
      changed_at        DATETIME2(3)  NOT NULL CONSTRAINT df_invoice_changes_at DEFAULT SYSUTCDATETIME(),
      changed_by        NVARCHAR(150) NULL,
      company_id        INT           NULL,
      supplier_ruc      NVARCHAR(11)  NULL,
      document_type     NVARCHAR(2)   NULL,
      series            NVARCHAR(10)  NULL,
      number            NVARCHAR(20)  NULL,
      issue_date        DATE          NULL,
      currency          NVARCHAR(3)   NULL,
      igv_cents         BIGINT        NULL,
      total_cents       BIGINT        NULL,
      validation_status NVARCHAR(10)  NULL,
      review_status     NVARCHAR(10)  NULL,
      reviewed_by       NVARCHAR(100) NULL,
      sunat_status      NVARCHAR(30)  NULL,
      corrected         BIT           NULL
    )""",
        "CREATE INDEX ix_invoice_changes_invoice ON invoice_changes (invoice_id, id)",
        """CREATE TRIGGER trg_invoices_audit ON invoices AFTER INSERT, UPDATE, DELETE AS
    BEGIN
      SET NOCOUNT ON;
      INSERT INTO invoice_changes (invoice_id, operation, changed_by, company_id, supplier_ruc,
        document_type, series, number, issue_date, currency, igv_cents, total_cents,
        validation_status, review_status, reviewed_by, sunat_status, corrected)
      SELECT COALESCE(i.id, d.id),
        CASE WHEN d.id IS NULL THEN N'I' WHEN i.id IS NULL THEN N'D' ELSE N'U' END,
        COALESCE(CAST(SESSION_CONTEXT(N'invoicebot.actor') AS NVARCHAR(150)), ORIGINAL_LOGIN()),
        COALESCE(i.company_id, d.company_id), COALESCE(i.supplier_ruc, d.supplier_ruc),
        COALESCE(i.document_type, d.document_type), COALESCE(i.series, d.series),
        COALESCE(i.number, d.number), COALESCE(i.issue_date, d.issue_date),
        COALESCE(i.currency, d.currency), COALESCE(i.igv_cents, d.igv_cents),
        COALESCE(i.total_cents, d.total_cents),
        COALESCE(i.validation_status, d.validation_status),
        COALESCE(i.review_status, d.review_status), COALESCE(i.reviewed_by, d.reviewed_by),
        COALESCE(i.sunat_status, d.sunat_status), COALESCE(i.corrected, d.corrected)
      FROM inserted i FULL OUTER JOIN deleted d ON d.id = i.id;
    END""",
        """CREATE TABLE company_changes (
      id              BIGINT IDENTITY(1, 1) NOT NULL CONSTRAINT pk_company_changes PRIMARY KEY,
      company_id      INT           NOT NULL,
      operation       NCHAR(1)      NOT NULL,
      changed_at      DATETIME2(3)  NOT NULL CONSTRAINT df_company_changes_at DEFAULT SYSUTCDATETIME(),
      changed_by      NVARCHAR(150) NULL,
      ruc             NVARCHAR(11)  NULL,
      name            NVARCHAR(200) NULL,
      retention_agent BIT           NULL,
      good_taxpayer   BIT           NULL,
      active          BIT           NULL
    )""",
        """CREATE TRIGGER trg_companies_audit ON companies AFTER INSERT, UPDATE, DELETE AS
    BEGIN
      SET NOCOUNT ON;
      INSERT INTO company_changes (company_id, operation, changed_by, ruc, name,
        retention_agent, good_taxpayer, active)
      SELECT COALESCE(i.id, d.id),
        CASE WHEN d.id IS NULL THEN N'I' WHEN i.id IS NULL THEN N'D' ELSE N'U' END,
        COALESCE(CAST(SESSION_CONTEXT(N'invoicebot.actor') AS NVARCHAR(150)), ORIGINAL_LOGIN()),
        COALESCE(i.ruc, d.ruc), COALESCE(i.name, d.name),
        COALESCE(i.retention_agent, d.retention_agent), COALESCE(i.good_taxpayer, d.good_taxpayer),
        COALESCE(i.active, d.active)
      FROM inserted i FULL OUTER JOIN deleted d ON d.id = i.id;
    END""",
        # Row-Level Security. Sin contexto (workers, CLI, programador) se ve todo; la API fija
        # SESSION_CONTEXT('invoicebot.companies') = '1,4' para quien está limitado a empresas.
        """CREATE FUNCTION dbo.fn_invoicebot_company_access (@company_id INT)
    RETURNS TABLE WITH SCHEMABINDING AS
    RETURN SELECT 1 AS allowed
    WHERE SESSION_CONTEXT(N'invoicebot.companies') IS NULL
       OR (@company_id IS NOT NULL AND CHARINDEX(
             N',' + CAST(@company_id AS NVARCHAR(12)) + N',',
             N',' + CAST(SESSION_CONTEXT(N'invoicebot.companies') AS NVARCHAR(4000)) + N',') > 0)""",
        """CREATE SECURITY POLICY dbo.invoicebot_company_policy
    ADD FILTER PREDICATE dbo.fn_invoicebot_company_access(company_id) ON dbo.invoices,
    ADD BLOCK PREDICATE dbo.fn_invoicebot_company_access(company_id) ON dbo.invoices AFTER UPDATE
    WITH (STATE = ON)""",
    ),
)

V1_MIGRATIONS = (M005, M006, M007)
