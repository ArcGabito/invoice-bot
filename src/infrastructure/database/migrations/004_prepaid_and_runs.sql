-- Anticipos aplicados al comprobante: el total a pagar ya los descuenta.
ALTER TABLE invoices ADD COLUMN prepaid_amount_cents INTEGER;

-- Historial de ejecuciones desatendidas (`invoicebot run`): permite saber
-- cuándo corrió el bot, qué hizo y si terminó bien, sin revisar los logs.
CREATE TABLE bot_runs (
  id            INTEGER PRIMARY KEY,
  trigger       TEXT    NOT NULL CHECK (trigger IN ('manual', 'scheduled', 'watch')),
  status        TEXT    NOT NULL CHECK (status IN ('running', 'success', 'partial', 'failed')),
  exit_code     INTEGER,
  summary       TEXT,
  error_message TEXT,
  host          TEXT,
  pid           INTEGER,
  started_at    TEXT    NOT NULL,
  finished_at   TEXT
);

CREATE INDEX idx_bot_runs_started ON bot_runs (started_at);
