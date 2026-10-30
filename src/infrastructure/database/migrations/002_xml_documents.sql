-- Comprobantes electrónicos en XML (UBL 2.1).
--
-- documents:
--   - status 'skipped': documentos que no se procesan por diseño, como la
--     representación impresa (PDF) de un XML ya leído o la constancia de
--     recepción (CDR) de SUNAT. skip_reason guarda el motivo.
--   - text_method 'xml': datos leídos del XML, sin IA.
--   - linked_document_id: el XML del que un PDF es representación impresa.
-- SQLite no permite cambiar un CHECK, así que la tabla se reconstruye.

CREATE TABLE documents_new (
  id                 INTEGER PRIMARY KEY,
  sha256             TEXT    NOT NULL UNIQUE,
  file_path          TEXT    NOT NULL,
  file_name          TEXT    NOT NULL,
  mime_type          TEXT    NOT NULL,
  file_size          INTEGER NOT NULL,
  status             TEXT    NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'processing', 'extracted', 'failed', 'needs_ocr', 'skipped')),
  text_method        TEXT    CHECK (text_method IN ('pdf_text', 'ocr', 'xml')),
  text_chars         INTEGER,
  page_count         INTEGER,
  attempts           INTEGER NOT NULL DEFAULT 0,
  error_kind         TEXT,
  error_message      TEXT,
  skip_reason        TEXT,
  linked_document_id INTEGER REFERENCES documents (id),
  created_at         TEXT    NOT NULL,
  updated_at         TEXT    NOT NULL,
  processed_at       TEXT
);

INSERT INTO documents_new
  (id, sha256, file_path, file_name, mime_type, file_size, status, text_method, text_chars,
   page_count, attempts, error_kind, error_message, created_at, updated_at, processed_at)
SELECT id, sha256, file_path, file_name, mime_type, file_size, status, text_method, text_chars,
   page_count, attempts, error_kind, error_message, created_at, updated_at, processed_at
FROM documents;

DROP TABLE documents;
ALTER TABLE documents_new RENAME TO documents;
CREATE INDEX idx_documents_status ON documents (status);
CREATE INDEX idx_documents_linked ON documents (linked_document_id);

-- Origen de los datos de cada factura: XML (exacto) o IA (propuesto).
ALTER TABLE invoices ADD COLUMN data_source TEXT NOT NULL DEFAULT 'ai'
  CHECK (data_source IN ('ai', 'xml'));
