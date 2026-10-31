-- Revisión humana: aprobar, rechazar y corregir facturas, con auditoría.

-- pending: sin revisar · approved: una persona la confirmó · rejected: se
-- excluye de exportaciones y de la bandeja (p. ej. duplicado o no es un gasto).
ALTER TABLE invoices ADD COLUMN review_status TEXT NOT NULL DEFAULT 'pending'
  CHECK (review_status IN ('pending', 'approved', 'rejected'));
ALTER TABLE invoices ADD COLUMN reviewed_at TEXT;
ALTER TABLE invoices ADD COLUMN reviewed_by TEXT;
-- 1 si una persona corrigió algún dato extraído.
ALTER TABLE invoices ADD COLUMN corrected INTEGER NOT NULL DEFAULT 0;

ALTER TABLE validation_issues ADD COLUMN resolved_by TEXT;
ALTER TABLE validation_issues ADD COLUMN resolution_note TEXT;

-- Registro inmutable de cada acción de revisión: quién, cuándo y qué cambió.
CREATE TABLE review_actions (
  id         INTEGER PRIMARY KEY,
  invoice_id INTEGER NOT NULL REFERENCES invoices (id) ON DELETE CASCADE,
  action     TEXT    NOT NULL
             CHECK (action IN ('approve', 'reject', 'reopen', 'correct', 'resolve_issue')),
  field      TEXT,
  old_value  TEXT,
  new_value  TEXT,
  issue_id   INTEGER,
  note       TEXT,
  user       TEXT    NOT NULL,
  created_at TEXT    NOT NULL
);

CREATE INDEX idx_review_actions_invoice ON review_actions (invoice_id);
CREATE INDEX idx_invoices_review_status ON invoices (review_status);
