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

_NOW = "CONSTRAINT df_{0}_created DEFAULT SYSUTCDATETIME()"

#: Cuentas por pagar: órdenes de compra, recepciones, match a 2 y 3 vías, matriz de
#: aprobación con delegaciones, cuenta contable aprendida por proveedor, notas de crédito
#: aplicadas y eventos de integración (outbox + webhooks firmados).
M008 = Migration(
    8,
    "008_purchasing_approvals_events",
    (
        """ALTER TABLE invoices ADD
      -- not_required | pending | approved | rejected (autorización de pago)
      approval_status       NVARCHAR(12) NULL,
      account_code          NVARCHAR(20) NULL,
      cost_center           NVARCHAR(30) NULL,
      -- history (aprendida) | order (de la orden de compra) | manual
      accounting_source     NVARCHAR(12) NULL,
      -- Notas de crédito y débito: la factura a la que se aplican.
      applied_to_invoice_id INT NULL""",
        "CREATE INDEX ix_invoices_approval ON invoices (approval_status) WHERE approval_status IS NOT NULL",
        "CREATE INDEX ix_invoices_applied ON invoices (applied_to_invoice_id) WHERE applied_to_invoice_id IS NOT NULL",
        f"""CREATE TABLE purchase_orders (
      id               INT IDENTITY(1, 1) NOT NULL CONSTRAINT pk_purchase_orders PRIMARY KEY,
      company_id       INT NULL CONSTRAINT fk_purchase_orders_company REFERENCES companies (id),
      company_key      AS ISNULL(company_id, 0) PERSISTED,
      po_number        NVARCHAR(40)  NOT NULL,
      supplier_ruc     NVARCHAR(11)  NOT NULL,
      supplier_name    NVARCHAR(300) NULL,
      currency         NVARCHAR(3)   NOT NULL CONSTRAINT df_purchase_orders_currency DEFAULT N'PEN',
      issue_date       DATE          NULL,
      total_cents      BIGINT        NOT NULL CONSTRAINT df_purchase_orders_total DEFAULT 0,
      status           NVARCHAR(20)  NOT NULL CONSTRAINT df_purchase_orders_status DEFAULT N'open'
        CONSTRAINT ck_purchase_orders_status CHECK (status IN (N'open', N'closed', N'cancelled')),
      requires_receipt BIT           NOT NULL CONSTRAINT df_purchase_orders_receipt DEFAULT 1,
      cost_center      NVARCHAR(30)  NULL,
      requester        NVARCHAR(150) NULL,
      source           NVARCHAR(20)  NOT NULL CONSTRAINT df_purchase_orders_source DEFAULT N'api',
      created_at       DATETIME2(3)  NOT NULL {_NOW.format("purchase_orders")},
      updated_at       DATETIME2(3)  NOT NULL CONSTRAINT df_purchase_orders_updated DEFAULT SYSUTCDATETIME(),
      CONSTRAINT uq_purchase_orders_number UNIQUE (company_key, po_number)
    )""",
        "CREATE INDEX ix_purchase_orders_supplier ON purchase_orders (supplier_ruc, status)",
        """CREATE TABLE purchase_order_lines (
      id               INT IDENTITY(1, 1) NOT NULL CONSTRAINT pk_purchase_order_lines PRIMARY KEY,
      order_id         INT NOT NULL CONSTRAINT fk_po_lines_order REFERENCES purchase_orders (id) ON DELETE CASCADE,
      line_no          INT NOT NULL,
      item_code        NVARCHAR(60)   NULL,
      description      NVARCHAR(300)  NULL,
      quantity         DECIMAL(18, 4) NOT NULL,
      unit_price_cents BIGINT         NOT NULL,
      CONSTRAINT uq_po_lines UNIQUE (order_id, line_no)
    )""",
        f"""CREATE TABLE goods_receipts (
      id                 INT IDENTITY(1, 1) NOT NULL CONSTRAINT pk_goods_receipts PRIMARY KEY,
      company_id         INT NULL CONSTRAINT fk_goods_receipts_company REFERENCES companies (id),
      company_key        AS ISNULL(company_id, 0) PERSISTED,
      order_id           INT NULL CONSTRAINT fk_goods_receipts_order REFERENCES purchase_orders (id),
      receipt_number     NVARCHAR(40) NOT NULL,
      supplier_ruc       NVARCHAR(11) NOT NULL,
      receipt_date       DATE         NULL,
      despatch_reference NVARCHAR(40) NULL,
      amount_cents       BIGINT       NULL,
      source             NVARCHAR(20) NOT NULL CONSTRAINT df_goods_receipts_source DEFAULT N'api',
      created_at         DATETIME2(3) NOT NULL {_NOW.format("goods_receipts")},
      CONSTRAINT uq_goods_receipts UNIQUE (company_key, supplier_ruc, receipt_number)
    )""",
        "CREATE INDEX ix_goods_receipts_order ON goods_receipts (order_id)",
        "CREATE INDEX ix_goods_receipts_despatch ON goods_receipts (supplier_ruc, despatch_reference) WHERE despatch_reference IS NOT NULL",
        """CREATE TABLE goods_receipt_lines (
      id            INT IDENTITY(1, 1) NOT NULL CONSTRAINT pk_goods_receipt_lines PRIMARY KEY,
      receipt_id    INT NOT NULL CONSTRAINT fk_gr_lines_receipt REFERENCES goods_receipts (id) ON DELETE CASCADE,
      order_line_id INT NULL CONSTRAINT fk_gr_lines_po_line REFERENCES purchase_order_lines (id),
      item_code     NVARCHAR(60)   NULL,
      quantity      DECIMAL(18, 4) NOT NULL
    )""",
        """CREATE TABLE invoice_matches (
      invoice_id    INT NOT NULL CONSTRAINT pk_invoice_matches PRIMARY KEY
                    CONSTRAINT fk_invoice_matches_invoice REFERENCES invoices (id) ON DELETE CASCADE,
      order_id      INT NULL CONSTRAINT fk_invoice_matches_order REFERENCES purchase_orders (id),
      status        NVARCHAR(12)  NOT NULL CONSTRAINT ck_invoice_matches_status
        CHECK (status IN (N'matched', N'exception', N'no_order')),
      match_type    NVARCHAR(10)  NULL,
      found_by      NVARCHAR(20)  NULL,
      matched_cents BIGINT        NULL,
      exceptions    NVARCHAR(MAX) NULL,
      checked_at    DATETIME2(3)  NOT NULL CONSTRAINT df_invoice_matches_checked DEFAULT SYSUTCDATETIME()
    )""",
        "CREATE INDEX ix_invoice_matches_order ON invoice_matches (order_id) WHERE order_id IS NOT NULL",
        f"""CREATE TABLE approval_rules (
      id               INT IDENTITY(1, 1) NOT NULL CONSTRAINT pk_approval_rules PRIMARY KEY,
      company_id       INT NULL CONSTRAINT fk_approval_rules_company REFERENCES companies (id),
      cost_center      NVARCHAR(30) NULL,
      supplier_ruc     NVARCHAR(11) NULL,
      min_amount_cents BIGINT       NOT NULL CONSTRAINT df_approval_rules_min DEFAULT 0,
      max_amount_cents BIGINT       NULL,
      level            INT          NOT NULL CONSTRAINT df_approval_rules_level DEFAULT 1,
      approver         NVARCHAR(40) NULL,
      sla_hours        INT          NOT NULL CONSTRAINT df_approval_rules_sla DEFAULT 48,
      escalate_to      NVARCHAR(40) NULL,
      active           BIT          NOT NULL CONSTRAINT df_approval_rules_active DEFAULT 1,
      created_at       DATETIME2(3) NOT NULL {_NOW.format("approval_rules")}
    )""",
        f"""CREATE TABLE approval_requests (
      id               INT IDENTITY(1, 1) NOT NULL CONSTRAINT pk_approval_requests PRIMARY KEY,
      invoice_id       INT NOT NULL CONSTRAINT fk_approval_requests_invoice REFERENCES invoices (id) ON DELETE CASCADE,
      level            INT           NOT NULL,
      rule_id          INT           NULL,
      approver         NVARCHAR(40)  NOT NULL,
      status           NVARCHAR(12)  NOT NULL CONSTRAINT df_approval_requests_status DEFAULT N'pending'
        CONSTRAINT ck_approval_requests_status
        CHECK (status IN (N'pending', N'approved', N'rejected', N'escalated', N'cancelled')),
      due_at           DATETIME2(3)  NOT NULL,
      escalate_to      NVARCHAR(40)  NULL,
      reminders_sent   INT           NOT NULL CONSTRAINT df_approval_requests_reminders DEFAULT 0,
      last_reminder_at DATETIME2(3)  NULL,
      decided_at       DATETIME2(3)  NULL,
      decided_by       NVARCHAR(150) NULL,
      comment          NVARCHAR(1000) NULL,
      token_hash       BINARY(32)    NULL,
      token_expires_at DATETIME2(3)  NULL,
      created_at       DATETIME2(3)  NOT NULL {_NOW.format("approval_requests")}
    )""",
        "CREATE INDEX ix_approval_requests_pending ON approval_requests (status, due_at) INCLUDE (approver)",
        "CREATE INDEX ix_approval_requests_invoice ON approval_requests (invoice_id, level)",
        "CREATE UNIQUE INDEX uq_approval_requests_token ON approval_requests (token_hash) WHERE token_hash IS NOT NULL",
        f"""CREATE TABLE approval_delegations (
      id         INT IDENTITY(1, 1) NOT NULL CONSTRAINT pk_approval_delegations PRIMARY KEY,
      username   NVARCHAR(40)  NOT NULL,
      delegate   NVARCHAR(40)  NOT NULL,
      starts_on  DATE          NOT NULL,
      ends_on    DATE          NOT NULL,
      reason     NVARCHAR(300) NULL,
      created_at DATETIME2(3)  NOT NULL {_NOW.format("approval_delegations")},
      CONSTRAINT ck_approval_delegations_dates CHECK (ends_on >= starts_on)
    )""",
        """CREATE TABLE supplier_accounting (
      id           INT IDENTITY(1, 1) NOT NULL CONSTRAINT pk_supplier_accounting PRIMARY KEY,
      company_key  INT          NOT NULL CONSTRAINT df_supplier_accounting_company DEFAULT 0,
      supplier_ruc NVARCHAR(11) NOT NULL,
      account_code NVARCHAR(20) NULL,
      cost_center  NVARCHAR(30) NULL,
      uses         INT          NOT NULL CONSTRAINT df_supplier_accounting_uses DEFAULT 1,
      last_used_at DATETIME2(3) NOT NULL CONSTRAINT df_supplier_accounting_used DEFAULT SYSUTCDATETIME()
    )""",
        "CREATE INDEX ix_supplier_accounting ON supplier_accounting (company_key, supplier_ruc)",
        f"""CREATE TABLE webhooks (
      id         INT IDENTITY(1, 1) NOT NULL CONSTRAINT pk_webhooks PRIMARY KEY,
      name       NVARCHAR(100) NOT NULL,
      url        NVARCHAR(500) NOT NULL,
      secret     NVARCHAR(100) NOT NULL,
      events     NVARCHAR(500) NOT NULL CONSTRAINT df_webhooks_events DEFAULT N'*',
      company_id INT NULL CONSTRAINT fk_webhooks_company REFERENCES companies (id),
      active     BIT           NOT NULL CONSTRAINT df_webhooks_active DEFAULT 1,
      created_at DATETIME2(3)  NOT NULL {_NOW.format("webhooks")}
    )""",
        f"""CREATE TABLE outbox_events (
      id         BIGINT IDENTITY(1, 1) NOT NULL CONSTRAINT pk_outbox_events PRIMARY KEY,
      event_id   NVARCHAR(36)  NOT NULL CONSTRAINT uq_outbox_events_event UNIQUE,
      type       NVARCHAR(80)  NOT NULL,
      subject    NVARCHAR(100) NULL,
      company_id INT           NULL,
      data       NVARCHAR(MAX) NOT NULL,
      created_at DATETIME2(3)  NOT NULL {_NOW.format("outbox_events")}
    )""",
        "CREATE INDEX ix_outbox_events_type ON outbox_events (type, id DESC)",
        f"""CREATE TABLE webhook_deliveries (
      id               BIGINT IDENTITY(1, 1) NOT NULL CONSTRAINT pk_webhook_deliveries PRIMARY KEY,
      event_id         BIGINT NOT NULL CONSTRAINT fk_deliveries_event REFERENCES outbox_events (id) ON DELETE CASCADE,
      webhook_id       INT    NOT NULL CONSTRAINT fk_deliveries_webhook REFERENCES webhooks (id) ON DELETE CASCADE,
      status           NVARCHAR(12)   NOT NULL CONSTRAINT df_deliveries_status DEFAULT N'pending'
        CONSTRAINT ck_deliveries_status CHECK (status IN (N'pending', N'delivered', N'failed')),
      attempts         INT            NOT NULL CONSTRAINT df_deliveries_attempts DEFAULT 0,
      last_status_code INT            NULL,
      last_error       NVARCHAR(1000) NULL,
      delivered_at     DATETIME2(3)   NULL,
      created_at       DATETIME2(3)   NOT NULL {_NOW.format("webhook_deliveries")}
    )""",
        "CREATE INDEX ix_webhook_deliveries_pending ON webhook_deliveries (status, id) WHERE status = N'pending'",
        # La auditoría registra también la autorización de pago y la cuenta contable.
        "ALTER TABLE invoice_changes ADD approval_status NVARCHAR(12) NULL, account_code NVARCHAR(20) NULL, cost_center NVARCHAR(30) NULL",
        """CREATE OR ALTER TRIGGER trg_invoices_audit ON invoices AFTER INSERT, UPDATE, DELETE AS
    BEGIN
      SET NOCOUNT ON;
      INSERT INTO invoice_changes (invoice_id, operation, changed_by, company_id, supplier_ruc,
        document_type, series, number, issue_date, currency, igv_cents, total_cents,
        validation_status, review_status, reviewed_by, sunat_status, corrected,
        approval_status, account_code, cost_center)
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
        COALESCE(i.sunat_status, d.sunat_status), COALESCE(i.corrected, d.corrected),
        COALESCE(i.approval_status, d.approval_status), COALESCE(i.account_code, d.account_code),
        COALESCE(i.cost_center, d.cost_center)
      FROM inserted i FULL OUTER JOIN deleted d ON d.id = i.id;
    END""",
        # Saldo de cada factura después de sus notas de crédito (07) y débito (08).
        """CREATE VIEW v_invoice_balances AS
    SELECT i.id AS invoice_id, i.company_id, i.supplier_ruc, i.series, i.number, i.currency,
      i.total_cents,
      COALESCE(SUM(CASE WHEN n.document_type = N'07' THEN n.total_cents END), 0) AS credit_cents,
      COALESCE(SUM(CASE WHEN n.document_type = N'08' THEN n.total_cents END), 0) AS debit_cents,
      i.total_cents
        - COALESCE(SUM(CASE WHEN n.document_type = N'07' THEN n.total_cents END), 0)
        + COALESCE(SUM(CASE WHEN n.document_type = N'08' THEN n.total_cents END), 0) AS balance_cents
    FROM invoices i
    LEFT JOIN invoices n ON n.applied_to_invoice_id = i.id AND n.review_status <> N'rejected'
    WHERE i.document_type NOT IN (N'07', N'08')
    GROUP BY i.id, i.company_id, i.supplier_ruc, i.series, i.number, i.currency, i.total_cents""",
    ),
)

#: Cumplimiento tributario: propuesta del SIRE (Registro de Compras), constancias de
#: depósito de detracciones y condición tributaria de los proveedores (retenciones).
M009 = Migration(
    9,
    "009_tax_compliance",
    (
        f"""CREATE TABLE sire_proposals (
      id          INT IDENTITY(1, 1) NOT NULL CONSTRAINT pk_sire_proposals PRIMARY KEY,
      company_id  INT NULL CONSTRAINT fk_sire_proposals_company REFERENCES companies (id),
      period      NCHAR(6)      NOT NULL,
      source      NVARCHAR(10)  NOT NULL CONSTRAINT df_sire_proposals_source DEFAULT N'file',
      file_name   NVARCHAR(260) NULL,
      row_count   INT           NOT NULL CONSTRAINT df_sire_proposals_rows DEFAULT 0,
      created_by  NVARCHAR(150) NULL,
      created_at  DATETIME2(3)  NOT NULL {_NOW.format("sire_proposals")}
    )""",
        "CREATE INDEX ix_sire_proposals_period ON sire_proposals (period, company_id, id DESC)",
        """CREATE TABLE sire_entries (
      id            BIGINT IDENTITY(1, 1) NOT NULL CONSTRAINT pk_sire_entries PRIMARY KEY,
      proposal_id   INT NOT NULL CONSTRAINT fk_sire_entries_proposal REFERENCES sire_proposals (id) ON DELETE CASCADE,
      supplier_ruc  NVARCHAR(15)  NULL,
      document_type NVARCHAR(2)   NULL,
      series        NVARCHAR(20)  NULL,
      number        NVARCHAR(20)  NULL,
      issue_date    DATE          NULL,
      currency      NVARCHAR(3)   NULL,
      total_cents   BIGINT        NULL,
      igv_cents     BIGINT        NULL,
      car           NVARCHAR(40)  NULL,
      raw           NVARCHAR(MAX) NULL
    )""",
        "CREATE INDEX ix_sire_entries_proposal ON sire_entries (proposal_id)",
        "CREATE INDEX ix_sire_entries_key ON sire_entries (supplier_ruc, document_type, series, number)",
        f"""CREATE TABLE detraction_deposits (
      id           INT IDENTITY(1, 1) NOT NULL CONSTRAINT pk_detraction_deposits PRIMARY KEY,
      invoice_id   INT NOT NULL CONSTRAINT fk_detraction_deposits_invoice REFERENCES invoices (id) ON DELETE CASCADE,
      constancia   NVARCHAR(20)  NOT NULL CONSTRAINT uq_detraction_deposits UNIQUE,
      deposit_date DATE          NOT NULL,
      amount_cents BIGINT        NOT NULL,
      source       NVARCHAR(20)  NOT NULL CONSTRAINT df_detraction_deposits_source DEFAULT N'manual',
      created_by   NVARCHAR(150) NULL,
      created_at   DATETIME2(3)  NOT NULL {_NOW.format("detraction_deposits")}
    )""",
        "CREATE INDEX ix_detraction_deposits_invoice ON detraction_deposits (invoice_id)",
        """CREATE TABLE supplier_tax_status (
      ruc               NVARCHAR(11) NOT NULL CONSTRAINT pk_supplier_tax_status PRIMARY KEY,
      good_taxpayer     BIT          NOT NULL CONSTRAINT df_sts_good DEFAULT 0,
      retention_agent   BIT          NOT NULL CONSTRAINT df_sts_retention DEFAULT 0,
      perception_agent  BIT          NOT NULL CONSTRAINT df_sts_perception DEFAULT 0,
      source            NVARCHAR(20) NOT NULL CONSTRAINT df_sts_source DEFAULT N'manual',
      updated_at        DATETIME2(3) NOT NULL CONSTRAINT df_sts_updated DEFAULT SYSUTCDATETIME()
    )""",
    ),
)

V1_MIGRATIONS = (M005, M006, M007, M008, M009)
