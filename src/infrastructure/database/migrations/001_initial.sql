-- Esquema inicial de InvoiceBot.
-- Los importes se guardan como enteros en céntimos (*_cents).
-- Las fechas de auditoría se guardan en ISO 8601 UTC.

CREATE TABLE documents (
  id            INTEGER PRIMARY KEY,
  sha256        TEXT    NOT NULL UNIQUE,
  file_path     TEXT    NOT NULL,
  file_name     TEXT    NOT NULL,
  mime_type     TEXT    NOT NULL,
  file_size     INTEGER NOT NULL,
  status        TEXT    NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'processing', 'extracted', 'failed', 'needs_ocr')),
  text_method   TEXT    CHECK (text_method IN ('pdf_text', 'ocr')),
  text_chars    INTEGER,
  page_count    INTEGER,
  attempts      INTEGER NOT NULL DEFAULT 0,
  error_kind    TEXT,
  error_message TEXT,
  created_at    TEXT    NOT NULL,
  updated_at    TEXT    NOT NULL,
  processed_at  TEXT
);

CREATE INDEX idx_documents_status ON documents (status);

-- Cada ruta en la que se ha visto un archivo. Varias rutas con el mismo
-- documento (mismo SHA-256) son duplicados exactos.
CREATE TABLE file_occurrences (
  id            INTEGER PRIMARY KEY,
  document_id   INTEGER NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
  file_path     TEXT    NOT NULL UNIQUE,
  first_seen_at TEXT    NOT NULL,
  last_seen_at  TEXT    NOT NULL
);

CREATE INDEX idx_file_occurrences_document ON file_occurrences (document_id);

CREATE TABLE extraction_runs (
  id             INTEGER PRIMARY KEY,
  document_id    INTEGER NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
  attempt        INTEGER NOT NULL,
  provider       TEXT    NOT NULL,
  model          TEXT,
  prompt_version TEXT    NOT NULL,
  text_method    TEXT,
  input_chars    INTEGER,
  status         TEXT    NOT NULL CHECK (status IN ('success', 'failed')),
  error_kind     TEXT,
  error_message  TEXT,
  raw_response   TEXT,
  input_tokens   INTEGER,
  output_tokens  INTEGER,
  duration_ms    INTEGER,
  created_at     TEXT    NOT NULL
);

CREATE INDEX idx_extraction_runs_document ON extraction_runs (document_id);

-- Una factura vigente por documento. Las re-extracciones la reemplazan;
-- el historial completo queda en extraction_runs.
CREATE TABLE invoices (
  id                  INTEGER PRIMARY KEY,
  document_id         INTEGER NOT NULL UNIQUE REFERENCES documents (id) ON DELETE CASCADE,
  extraction_run_id   INTEGER NOT NULL REFERENCES extraction_runs (id),
  supplier_name       TEXT,
  supplier_ruc        TEXT,
  customer_name       TEXT,
  customer_ruc        TEXT,
  document_type       TEXT,
  series              TEXT,
  number              TEXT,
  issue_date          TEXT,
  due_date            TEXT,
  currency            TEXT,
  subtotal_cents          INTEGER,
  taxable_amount_cents    INTEGER,
  exempt_amount_cents     INTEGER,
  unaffected_amount_cents INTEGER,
  discount_amount_cents   INTEGER,
  igv_cents               INTEGER,
  isc_cents               INTEGER,
  icbper_cents            INTEGER,
  other_charges_cents     INTEGER,
  other_taxes_cents       INTEGER,
  total_cents             INTEGER,
  business_key        TEXT,
  validation_status   TEXT    NOT NULL CHECK (validation_status IN ('valid', 'warning', 'error')),
  rules_version       TEXT    NOT NULL,
  created_at          TEXT    NOT NULL,
  updated_at          TEXT    NOT NULL
);

-- Índice (no único) para detectar posibles duplicados: los datos extraídos
-- pueden ser incorrectos y necesitan revisión antes de descartar nada.
CREATE INDEX idx_invoices_business_key ON invoices (business_key);
CREATE INDEX idx_invoices_supplier_doc ON invoices (supplier_ruc, document_type, series, number);
CREATE INDEX idx_invoices_issue_date ON invoices (issue_date);

CREATE TABLE validation_issues (
  id             INTEGER PRIMARY KEY,
  invoice_id     INTEGER NOT NULL REFERENCES invoices (id) ON DELETE CASCADE,
  rule_code      TEXT    NOT NULL,
  severity       TEXT    NOT NULL CHECK (severity IN ('error', 'warning', 'info')),
  message        TEXT    NOT NULL,
  expected_value TEXT,
  actual_value   TEXT,
  resolved_at    TEXT
);

CREATE INDEX idx_validation_issues_invoice ON validation_issues (invoice_id);
