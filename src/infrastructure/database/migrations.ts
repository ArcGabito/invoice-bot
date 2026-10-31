/**
 * Migraciones del esquema en T-SQL (SQL Server 2019 o superior).
 *
 * Cada migración es una lista de lotes: SQL Server no admite `GO` desde un
 * driver, y algunas sentencias (CREATE VIEW) deben ir solas en su lote.
 * Los lotes de una migración se aplican en una transacción.
 *
 * Convenciones:
 * - Importes en céntimos (BIGINT, columnas *_cents); la vista v_invoices los
 *   ofrece en DECIMAL(18,2) para informes (Power BI, Excel).
 * - Texto en NVARCHAR: los parámetros del driver son NVARCHAR y compararlos
 *   con columnas VARCHAR obligaría a convertir la columna y perder los índices.
 * - Fechas de auditoría en DATETIME2(3) UTC; fechas de comprobante en DATE.
 */
export interface Migration {
  version: number;
  name: string;
  batches: string[];
}

const initial: Migration = {
  version: 1,
  name: '001_initial',
  batches: [
    `CREATE TABLE documents (
      id                 INT IDENTITY(1, 1) NOT NULL CONSTRAINT pk_documents PRIMARY KEY,
      sha256             NVARCHAR(64)   NOT NULL CONSTRAINT uq_documents_sha256 UNIQUE,
      file_path          NVARCHAR(1024) NOT NULL,
      file_name          NVARCHAR(260)  NOT NULL,
      mime_type          NVARCHAR(100)  NOT NULL,
      file_size          INT            NOT NULL,
      status             NVARCHAR(20)   NOT NULL CONSTRAINT df_documents_status DEFAULT N'pending'
                         CONSTRAINT ck_documents_status
                         CHECK (status IN (N'pending', N'processing', N'extracted', N'failed', N'needs_ocr', N'skipped')),
      text_method        NVARCHAR(20)   NULL
                         CONSTRAINT ck_documents_text_method CHECK (text_method IN (N'pdf_text', N'ocr', N'xml')),
      text_chars         INT            NULL,
      page_count         INT            NULL,
      attempts           INT            NOT NULL CONSTRAINT df_documents_attempts DEFAULT 0,
      error_kind         NVARCHAR(50)   NULL,
      error_message      NVARCHAR(4000) NULL,
      skip_reason        NVARCHAR(50)   NULL,
      -- El XML del que este PDF es la representación impresa.
      linked_document_id INT            NULL CONSTRAINT fk_documents_linked REFERENCES documents (id),
      created_at         DATETIME2(3)   NOT NULL CONSTRAINT df_documents_created DEFAULT SYSUTCDATETIME(),
      updated_at         DATETIME2(3)   NOT NULL CONSTRAINT df_documents_updated DEFAULT SYSUTCDATETIME(),
      processed_at       DATETIME2(3)   NULL
    )`,
    `CREATE INDEX ix_documents_status ON documents (status)`,
    `CREATE INDEX ix_documents_linked ON documents (linked_document_id)`,

    // Cada ruta en la que se vio un archivo. Varias rutas con el mismo
    // documento (mismo SHA-256) son duplicados exactos. La ruta puede superar
    // el límite de una clave de índice, así que la unicidad va sobre su hash.
    `CREATE TABLE file_occurrences (
      id            INT IDENTITY(1, 1) NOT NULL CONSTRAINT pk_file_occurrences PRIMARY KEY,
      document_id   INT            NOT NULL
                    CONSTRAINT fk_file_occurrences_document REFERENCES documents (id) ON DELETE CASCADE,
      file_path     NVARCHAR(1024) NOT NULL,
      path_hash     AS CAST(HASHBYTES('SHA2_256', file_path) AS BINARY(32)) PERSISTED,
      first_seen_at DATETIME2(3)   NOT NULL CONSTRAINT df_file_occurrences_first DEFAULT SYSUTCDATETIME(),
      last_seen_at  DATETIME2(3)   NOT NULL CONSTRAINT df_file_occurrences_last DEFAULT SYSUTCDATETIME()
    )`,
    `CREATE UNIQUE INDEX ux_file_occurrences_path ON file_occurrences (path_hash)`,
    `CREATE INDEX ix_file_occurrences_document ON file_occurrences (document_id)`,

    `CREATE TABLE extraction_runs (
      id             INT IDENTITY(1, 1) NOT NULL CONSTRAINT pk_extraction_runs PRIMARY KEY,
      document_id    INT            NOT NULL
                     CONSTRAINT fk_extraction_runs_document REFERENCES documents (id) ON DELETE CASCADE,
      attempt        INT            NOT NULL,
      provider       NVARCHAR(50)   NOT NULL,
      model          NVARCHAR(100)  NULL,
      prompt_version NVARCHAR(100)  NOT NULL,
      text_method    NVARCHAR(20)   NULL,
      input_chars    INT            NULL,
      status         NVARCHAR(10)   NOT NULL CONSTRAINT ck_extraction_runs_status CHECK (status IN (N'success', N'failed')),
      error_kind     NVARCHAR(50)   NULL,
      error_message  NVARCHAR(4000) NULL,
      raw_response   NVARCHAR(MAX)  NULL,
      input_tokens   INT            NULL,
      output_tokens  INT            NULL,
      duration_ms    INT            NULL,
      created_at     DATETIME2(3)   NOT NULL CONSTRAINT df_extraction_runs_created DEFAULT SYSUTCDATETIME()
    )`,
    `CREATE INDEX ix_extraction_runs_document ON extraction_runs (document_id)`,

    // Una factura vigente por documento; las re-extracciones la reemplazan y
    // el historial completo queda en extraction_runs.
    `CREATE TABLE invoices (
      id                      INT IDENTITY(1, 1) NOT NULL CONSTRAINT pk_invoices PRIMARY KEY,
      document_id             INT           NOT NULL CONSTRAINT uq_invoices_document UNIQUE
                              CONSTRAINT fk_invoices_document REFERENCES documents (id) ON DELETE CASCADE,
      extraction_run_id       INT           NOT NULL
                              CONSTRAINT fk_invoices_extraction_run REFERENCES extraction_runs (id),
      supplier_name           NVARCHAR(300) NULL,
      supplier_ruc            NVARCHAR(11)  NULL,
      -- Proveedores del exterior (tipo 91): identificador fiscal y país ISO.
      supplier_tax_id         NVARCHAR(50)  NULL,
      supplier_country        NVARCHAR(2)   NULL,
      customer_name           NVARCHAR(300) NULL,
      customer_ruc            NVARCHAR(15)  NULL,
      document_type           NVARCHAR(2)   NULL,
      series                  NVARCHAR(10)  NULL,
      number                  NVARCHAR(20)  NULL,
      issue_date              DATE          NULL,
      due_date                DATE          NULL,
      currency                NVARCHAR(3)   NULL,
      subtotal_cents          BIGINT        NULL,
      taxable_amount_cents    BIGINT        NULL,
      exempt_amount_cents     BIGINT        NULL,
      unaffected_amount_cents BIGINT        NULL,
      discount_amount_cents   BIGINT        NULL,
      igv_cents               BIGINT        NULL,
      isc_cents               BIGINT        NULL,
      icbper_cents            BIGINT        NULL,
      other_charges_cents     BIGINT        NULL,
      other_taxes_cents       BIGINT        NULL,
      total_cents             BIGINT        NULL,
      -- Anticipos aplicados: el total a pagar ya los descuenta.
      prepaid_amount_cents    BIGINT        NULL,
      data_source             NVARCHAR(10)  NOT NULL CONSTRAINT df_invoices_data_source DEFAULT N'ai'
                              CONSTRAINT ck_invoices_data_source CHECK (data_source IN (N'ai', N'xml')),
      business_key            NVARCHAR(400) NULL,
      validation_status       NVARCHAR(10)  NOT NULL
                              CONSTRAINT ck_invoices_validation CHECK (validation_status IN (N'valid', N'warning', N'error')),
      rules_version           NVARCHAR(50)  NOT NULL,
      -- Último estado del comprobante en SUNAT (ACEPTADO, ANULADO, NO EXISTE…). NULL = sin consultar.
      sunat_status            NVARCHAR(30)  NULL,
      sunat_checked_at        DATETIME2(3)  NULL,
      review_status           NVARCHAR(10)  NOT NULL CONSTRAINT df_invoices_review DEFAULT N'pending'
                              CONSTRAINT ck_invoices_review CHECK (review_status IN (N'pending', N'approved', N'rejected')),
      reviewed_at             DATETIME2(3)  NULL,
      reviewed_by             NVARCHAR(100) NULL,
      -- 1 si una persona corrigió algún dato extraído.
      corrected               BIT           NOT NULL CONSTRAINT df_invoices_corrected DEFAULT 0,
      created_at              DATETIME2(3)  NOT NULL CONSTRAINT df_invoices_created DEFAULT SYSUTCDATETIME(),
      updated_at              DATETIME2(3)  NOT NULL CONSTRAINT df_invoices_updated DEFAULT SYSUTCDATETIME()
    )`,
    // No único: los datos extraídos pueden ser incorrectos y se revisan antes de descartar nada.
    `CREATE INDEX ix_invoices_business_key ON invoices (business_key)`,
    `CREATE INDEX ix_invoices_supplier_doc ON invoices (supplier_ruc, document_type, series, number)`,
    `CREATE INDEX ix_invoices_issue_date ON invoices (issue_date)`,
    `CREATE INDEX ix_invoices_review_status ON invoices (review_status)`,

    `CREATE TABLE validation_issues (
      id              INT IDENTITY(1, 1) NOT NULL CONSTRAINT pk_validation_issues PRIMARY KEY,
      invoice_id      INT            NOT NULL
                      CONSTRAINT fk_validation_issues_invoice REFERENCES invoices (id) ON DELETE CASCADE,
      rule_code       NVARCHAR(50)   NOT NULL,
      severity        NVARCHAR(10)   NOT NULL
                      CONSTRAINT ck_validation_issues_severity CHECK (severity IN (N'error', N'warning', N'info')),
      message         NVARCHAR(1000) NOT NULL,
      expected_value  NVARCHAR(1000) NULL,
      actual_value    NVARCHAR(1000) NULL,
      resolved_at     DATETIME2(3)   NULL,
      resolved_by     NVARCHAR(100)  NULL,
      resolution_note NVARCHAR(1000) NULL
    )`,
    `CREATE INDEX ix_validation_issues_invoice ON validation_issues (invoice_id)`,

    // Consultas a SUNAT (Consulta Integrada de Comprobante de Pago).
    `CREATE TABLE sunat_checks (
      id             INT IDENTITY(1, 1) NOT NULL CONSTRAINT pk_sunat_checks PRIMARY KEY,
      invoice_id     INT            NOT NULL
                     CONSTRAINT fk_sunat_checks_invoice REFERENCES invoices (id) ON DELETE CASCADE,
      status         NVARCHAR(10)   NOT NULL CONSTRAINT ck_sunat_checks_status CHECK (status IN (N'success', N'failed')),
      estado_cp      NVARCHAR(5)    NULL,
      estado_ruc     NVARCHAR(5)    NULL,
      cond_domi_ruc  NVARCHAR(5)    NULL,
      observations   NVARCHAR(MAX)  NULL,
      error_kind     NVARCHAR(50)   NULL,
      error_message  NVARCHAR(4000) NULL,
      raw_response   NVARCHAR(MAX)  NULL,
      checked_at     DATETIME2(3)   NOT NULL CONSTRAINT df_sunat_checks_checked DEFAULT SYSUTCDATETIME()
    )`,
    `CREATE INDEX ix_sunat_checks_invoice ON sunat_checks (invoice_id)`,

    // Registro inmutable de cada acción de revisión: quién, cuándo y qué cambió.
    `CREATE TABLE review_actions (
      id         INT IDENTITY(1, 1) NOT NULL CONSTRAINT pk_review_actions PRIMARY KEY,
      invoice_id INT            NOT NULL
                 CONSTRAINT fk_review_actions_invoice REFERENCES invoices (id) ON DELETE CASCADE,
      action     NVARCHAR(20)   NOT NULL
                 CONSTRAINT ck_review_actions_action
                 CHECK (action IN (N'approve', N'reject', N'reopen', N'correct', N'resolve_issue')),
      field      NVARCHAR(50)   NULL,
      old_value  NVARCHAR(1000) NULL,
      new_value  NVARCHAR(1000) NULL,
      issue_id   INT            NULL,
      note       NVARCHAR(1000) NULL,
      [user]     NVARCHAR(100)  NOT NULL,
      created_at DATETIME2(3)   NOT NULL CONSTRAINT df_review_actions_created DEFAULT SYSUTCDATETIME()
    )`,
    `CREATE INDEX ix_review_actions_invoice ON review_actions (invoice_id)`,

    // Historial de ejecuciones desatendidas (invoicebot run).
    `CREATE TABLE bot_runs (
      id            INT IDENTITY(1, 1) NOT NULL CONSTRAINT pk_bot_runs PRIMARY KEY,
      [trigger]     NVARCHAR(10)   NOT NULL
                    CONSTRAINT ck_bot_runs_trigger CHECK ([trigger] IN (N'manual', N'scheduled', N'watch')),
      status        NVARCHAR(10)   NOT NULL
                    CONSTRAINT ck_bot_runs_status CHECK (status IN (N'running', N'success', N'partial', N'failed')),
      exit_code     INT            NULL,
      summary       NVARCHAR(MAX)  NULL,
      error_message NVARCHAR(4000) NULL,
      host          NVARCHAR(255)  NULL,
      pid           INT            NULL,
      started_at    DATETIME2(3)   NOT NULL CONSTRAINT df_bot_runs_started DEFAULT SYSUTCDATETIME(),
      finished_at   DATETIME2(3)   NULL
    )`,
    `CREATE INDEX ix_bot_runs_started ON bot_runs (started_at)`,

    // Usuarios de la pantalla de revisión en red.
    `CREATE TABLE users (
      id            INT IDENTITY(1, 1) NOT NULL CONSTRAINT pk_users PRIMARY KEY,
      username      NVARCHAR(40)  NOT NULL CONSTRAINT uq_users_username UNIQUE,
      display_name  NVARCHAR(100) NOT NULL,
      -- scrypt$N$r$p$sal$hash (base64): nunca se guarda la contraseña.
      password_hash NVARCHAR(200) NOT NULL,
      active        BIT           NOT NULL CONSTRAINT df_users_active DEFAULT 1,
      created_at    DATETIME2(3)  NOT NULL CONSTRAINT df_users_created DEFAULT SYSUTCDATETIME(),
      updated_at    DATETIME2(3)  NOT NULL CONSTRAINT df_users_updated DEFAULT SYSUTCDATETIME(),
      last_login_at DATETIME2(3)  NULL
    )`,

    // Sesiones de la pantalla de revisión. Se guarda el hash del identificador
    // (no el identificador): quien lea la tabla no puede suplantar a nadie.
    // user_version es users.updated_at al entrar: si cambia (contraseña,
    // desactivación), la sesión deja de valer.
    `CREATE TABLE review_sessions (
      token_hash   BINARY(32)    NOT NULL CONSTRAINT pk_review_sessions PRIMARY KEY,
      user_id      INT           NOT NULL
                   CONSTRAINT fk_review_sessions_user REFERENCES users (id) ON DELETE CASCADE,
      user_version DATETIME2(3)  NOT NULL,
      csrf_token   NVARCHAR(64)  NOT NULL,
      ip           NVARCHAR(64)  NULL,
      created_at   DATETIME2(3)  NOT NULL CONSTRAINT df_review_sessions_created DEFAULT SYSUTCDATETIME(),
      expires_at   DATETIME2(3)  NOT NULL
    )`,
    `CREATE INDEX ix_review_sessions_expires ON review_sessions (expires_at)`,

    // Vista para informes: importes en soles/dólares (no céntimos) y datos del archivo.
    `CREATE VIEW v_invoices AS
    SELECT
      i.id AS invoice_id, i.document_id, d.file_name, d.file_path,
      i.supplier_name, i.supplier_ruc, i.supplier_tax_id, i.supplier_country,
      i.customer_name, i.customer_ruc, i.document_type, i.series, i.number,
      i.issue_date, i.due_date, i.currency,
      CAST(i.subtotal_cents / 100.0 AS DECIMAL(18, 2)) AS subtotal,
      CAST(i.taxable_amount_cents / 100.0 AS DECIMAL(18, 2)) AS taxable_amount,
      CAST(i.exempt_amount_cents / 100.0 AS DECIMAL(18, 2)) AS exempt_amount,
      CAST(i.unaffected_amount_cents / 100.0 AS DECIMAL(18, 2)) AS unaffected_amount,
      CAST(i.discount_amount_cents / 100.0 AS DECIMAL(18, 2)) AS discount_amount,
      CAST(i.igv_cents / 100.0 AS DECIMAL(18, 2)) AS igv,
      CAST(i.isc_cents / 100.0 AS DECIMAL(18, 2)) AS isc,
      CAST(i.icbper_cents / 100.0 AS DECIMAL(18, 2)) AS icbper,
      CAST(i.other_charges_cents / 100.0 AS DECIMAL(18, 2)) AS other_charges,
      CAST(i.other_taxes_cents / 100.0 AS DECIMAL(18, 2)) AS other_taxes,
      CAST(i.total_cents / 100.0 AS DECIMAL(18, 2)) AS total,
      CAST(i.prepaid_amount_cents / 100.0 AS DECIMAL(18, 2)) AS prepaid_amount,
      i.data_source, i.validation_status, i.review_status, i.reviewed_by, i.reviewed_at,
      i.sunat_status, i.sunat_checked_at, i.corrected, i.created_at, i.updated_at
    FROM invoices i
    JOIN documents d ON d.id = i.document_id`,
  ],
};

export const MIGRATIONS: Migration[] = [initial];
