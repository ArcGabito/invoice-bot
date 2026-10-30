-- Consultas de validez a SUNAT (Consulta Integrada de Comprobante de Pago).
-- Cada consulta queda registrada; la factura guarda el último resultado.

CREATE TABLE sunat_checks (
  id               INTEGER PRIMARY KEY,
  invoice_id       INTEGER NOT NULL REFERENCES invoices (id) ON DELETE CASCADE,
  status           TEXT    NOT NULL CHECK (status IN ('success', 'failed')),
  estado_cp        TEXT,
  estado_ruc       TEXT,
  cond_domi_ruc    TEXT,
  observations     TEXT,
  error_kind       TEXT,
  error_message    TEXT,
  raw_response     TEXT,
  checked_at       TEXT    NOT NULL
);

CREATE INDEX idx_sunat_checks_invoice ON sunat_checks (invoice_id);

-- Último estado del comprobante según SUNAT (ACEPTADO, ANULADO, NO EXISTE…)
-- y fecha de la última consulta correcta. NULL = sin consultar.
ALTER TABLE invoices ADD COLUMN sunat_status TEXT;
ALTER TABLE invoices ADD COLUMN sunat_checked_at TEXT;
