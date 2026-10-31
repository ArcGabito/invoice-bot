-- Usuarios de la pantalla de revisión cuando se usa desde la red
-- (`invoicebot serve` en una VM o un servidor). Cada persona entra con su
-- usuario, que es el nombre que queda en la auditoría de review_actions.

CREATE TABLE users (
  id            INTEGER PRIMARY KEY,
  username      TEXT    NOT NULL UNIQUE,
  display_name  TEXT    NOT NULL,
  -- scrypt$N$r$p$sal$hash (base64): nunca se guarda la contraseña.
  password_hash TEXT    NOT NULL,
  active        INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
  created_at    TEXT    NOT NULL,
  updated_at    TEXT    NOT NULL,
  last_login_at TEXT
);
