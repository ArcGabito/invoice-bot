import { AMOUNT_FIELDS, DATA_SOURCE, DOCUMENT_STATUS } from '../../../domain/invoice.js';

const INVOICE_COLUMNS = [
  'supplier_name',
  'supplier_ruc',
  'customer_name',
  'customer_ruc',
  'document_type',
  'series',
  'number',
  'issue_date',
  'due_date',
  'currency',
  ...AMOUNT_FIELDS.map((f) => `${f}_cents`),
  'data_source',
];

const now = () => new Date().toISOString();

/**
 * Implementación SQLite del puerto InvoiceRepository
 * (ver src/application/ports/invoice-repository.js).
 */
export class SqliteInvoiceRepository {
  /** @param {import('better-sqlite3').Database} db */
  constructor(db) {
    this.db = db;
  }

  // --- Documentos ---------------------------------------------------------

  findDocumentBySha(sha256) {
    return this.db.prepare('SELECT * FROM documents WHERE sha256 = ?').get(sha256) ?? null;
  }

  getDocument(id) {
    return this.db.prepare('SELECT * FROM documents WHERE id = ?').get(id) ?? null;
  }

  /** Documento visto por última vez en `filePath` (según file_occurrences). */
  findDocumentByPath(filePath) {
    return (
      this.db
        .prepare(
          `SELECT d.* FROM file_occurrences o JOIN documents d ON d.id = o.document_id
           WHERE o.file_path = ?`,
        )
        .get(filePath) ?? null
    );
  }

  insertDocument({ sha256, file_path, file_name, mime_type, file_size }) {
    const ts = now();
    const info = this.db
      .prepare(
        `INSERT INTO documents (sha256, file_path, file_name, mime_type, file_size, status, created_at, updated_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
      )
      .run(sha256, file_path, file_name, mime_type, file_size, DOCUMENT_STATUS.PENDING, ts, ts);
    return this.getDocument(info.lastInsertRowid);
  }

  /** Registra que el documento se vio en `filePath`. Devuelve true si la ruta es nueva. */
  recordOccurrence(documentId, filePath) {
    const ts = now();
    const existing = this.db
      .prepare('SELECT id, document_id FROM file_occurrences WHERE file_path = ?')
      .get(filePath);
    if (existing) {
      // El archivo de esa ruta pudo cambiar de contenido: se reasigna.
      this.db
        .prepare('UPDATE file_occurrences SET document_id = ?, last_seen_at = ? WHERE id = ?')
        .run(documentId, ts, existing.id);
      return existing.document_id !== documentId;
    }
    this.db
      .prepare(
        'INSERT INTO file_occurrences (document_id, file_path, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?)',
      )
      .run(documentId, filePath, ts, ts);
    return true;
  }

  getOccurrences(documentId) {
    return this.db
      .prepare(
        'SELECT file_path, first_seen_at, last_seen_at FROM file_occurrences WHERE document_id = ? ORDER BY id',
      )
      .all(documentId);
  }

  /** Marca el documento en proceso e incrementa su contador de intentos. */
  startAttempt(documentId) {
    this.db
      .prepare(
        `UPDATE documents SET status = ?, attempts = attempts + 1, error_kind = NULL, error_message = NULL, updated_at = ?
         WHERE id = ?`,
      )
      .run(DOCUMENT_STATUS.PROCESSING, now(), documentId);
    return this.db.prepare('SELECT attempts FROM documents WHERE id = ?').pluck().get(documentId);
  }

  updateDocument(documentId, fields) {
    const allowed = [
      'status',
      'text_method',
      'text_chars',
      'page_count',
      'error_kind',
      'error_message',
      'skip_reason',
      'linked_document_id',
      'processed_at',
      'file_path',
      'file_name',
    ];
    const entries = Object.entries(fields).filter(([k]) => allowed.includes(k));
    if (entries.length === 0) return;
    const sets = entries.map(([k]) => `${k} = ?`).join(', ');
    this.db
      .prepare(`UPDATE documents SET ${sets}, updated_at = ? WHERE id = ?`)
      .run(...entries.map(([, v]) => v ?? null), now(), documentId);
  }

  /** Documentos que quedaron "en proceso" por una ejecución interrumpida. */
  resetInterrupted() {
    return this.db
      .prepare('UPDATE documents SET status = ?, updated_at = ? WHERE status = ?')
      .run(DOCUMENT_STATUS.PENDING, now(), DOCUMENT_STATUS.PROCESSING).changes;
  }

  listDocumentsByStatus(statuses) {
    const placeholders = statuses.map(() => '?').join(', ');
    return this.db
      .prepare(`SELECT * FROM documents WHERE status IN (${placeholders}) ORDER BY id`)
      .all(...statuses);
  }

  // --- Extracciones --------------------------------------------------------

  insertExtractionRun(run) {
    const info = this.db
      .prepare(
        `INSERT INTO extraction_runs
           (document_id, attempt, provider, model, prompt_version, text_method, input_chars, status,
            error_kind, error_message, raw_response, input_tokens, output_tokens, duration_ms, created_at)
         VALUES (@document_id, @attempt, @provider, @model, @prompt_version, @text_method, @input_chars, @status,
            @error_kind, @error_message, @raw_response, @input_tokens, @output_tokens, @duration_ms, @created_at)`,
      )
      .run({
        error_kind: null,
        error_message: null,
        raw_response: null,
        input_tokens: null,
        output_tokens: null,
        duration_ms: null,
        text_method: null,
        input_chars: null,
        model: null,
        ...run,
        created_at: now(),
      });
    return Number(info.lastInsertRowid);
  }

  listExtractionRuns(documentId) {
    return this.db
      .prepare('SELECT * FROM extraction_runs WHERE document_id = ? ORDER BY id')
      .all(documentId);
  }

  // --- Facturas ------------------------------------------------------------

  /**
   * Guarda la factura, sus incidencias y el estado del documento en una sola
   * transacción. Si el documento ya tenía factura, se reemplaza.
   */
  saveInvoiceResult({
    documentId,
    extractionRunId,
    invoice,
    validationStatus,
    rulesVersion,
    businessKey,
    dataSource = DATA_SOURCE.AI,
    issues,
  }) {
    const ts = now();
    const tx = this.db.transaction(() => {
      const existing = this.db
        .prepare('SELECT id FROM invoices WHERE document_id = ?')
        .pluck()
        .get(documentId);
      const row = { ...invoice, data_source: dataSource };
      const values = INVOICE_COLUMNS.map((c) => row[c] ?? null);
      let invoiceId;
      if (existing) {
        const sets = INVOICE_COLUMNS.map((c) => `${c} = ?`).join(', ');
        this.db
          .prepare(
            `UPDATE invoices SET ${sets}, extraction_run_id = ?, business_key = ?, validation_status = ?,
               rules_version = ?, updated_at = ? WHERE id = ?`,
          )
          .run(
            ...values,
            extractionRunId,
            businessKey,
            validationStatus,
            rulesVersion,
            ts,
            existing,
          );
        this.db.prepare('DELETE FROM validation_issues WHERE invoice_id = ?').run(existing);
        invoiceId = existing;
      } else {
        const cols = [
          'document_id',
          'extraction_run_id',
          ...INVOICE_COLUMNS,
          'business_key',
          'validation_status',
          'rules_version',
          'created_at',
          'updated_at',
        ];
        const info = this.db
          .prepare(
            `INSERT INTO invoices (${cols.join(', ')}) VALUES (${cols.map(() => '?').join(', ')})`,
          )
          .run(
            documentId,
            extractionRunId,
            ...values,
            businessKey,
            validationStatus,
            rulesVersion,
            ts,
            ts,
          );
        invoiceId = Number(info.lastInsertRowid);
      }
      const insertIssue = this.db.prepare(
        `INSERT INTO validation_issues (invoice_id, rule_code, severity, message, expected_value, actual_value)
         VALUES (?, ?, ?, ?, ?, ?)`,
      );
      for (const i of issues) {
        insertIssue.run(
          invoiceId,
          i.rule_code,
          i.severity,
          i.message,
          i.expected_value ?? null,
          i.actual_value ?? null,
        );
      }
      this.updateDocument(documentId, {
        status: DOCUMENT_STATUS.EXTRACTED,
        error_kind: null,
        error_message: null,
        processed_at: ts,
      });
      return invoiceId;
    });
    return tx();
  }

  getInvoiceByDocument(documentId) {
    return this.db.prepare('SELECT * FROM invoices WHERE document_id = ?').get(documentId) ?? null;
  }

  getIssues(invoiceId) {
    return this.db
      .prepare('SELECT * FROM validation_issues WHERE invoice_id = ? ORDER BY id')
      .all(invoiceId);
  }

  /** Otras facturas con la misma clave de negocio, de documentos distintos. */
  findByBusinessKey(businessKey, excludeDocumentId) {
    return this.db
      .prepare(
        `SELECT i.id, i.document_id, d.file_name, d.file_path FROM invoices i
         JOIN documents d ON d.id = i.document_id
         WHERE i.business_key = ? AND i.document_id <> ? ORDER BY i.id`,
      )
      .all(businessKey, excludeDocumentId);
  }

  // --- Consultas -----------------------------------------------------------

  getStatusSummary() {
    const documents = Object.fromEntries(
      this.db
        .prepare('SELECT status, COUNT(*) AS n FROM documents GROUP BY status')
        .all()
        .map((r) => [r.status, r.n]),
    );
    const invoices = Object.fromEntries(
      this.db
        .prepare('SELECT validation_status, COUNT(*) AS n FROM invoices GROUP BY validation_status')
        .all()
        .map((r) => [r.validation_status, r.n]),
    );
    const exactDuplicates = this.db
      .prepare(
        'SELECT COUNT(*) FROM (SELECT document_id FROM file_occurrences GROUP BY document_id HAVING COUNT(*) > 1)',
      )
      .pluck()
      .get();
    const possibleDuplicates = this.db
      .prepare(
        `SELECT COUNT(*) FROM (SELECT business_key FROM invoices WHERE business_key IS NOT NULL
         GROUP BY business_key HAVING COUNT(*) > 1)`,
      )
      .pluck()
      .get();
    const runs = this.db
      .prepare(
        `SELECT COUNT(*) AS total, SUM(status = 'failed') AS failed,
           COALESCE(SUM(input_tokens), 0) AS input_tokens, COALESCE(SUM(output_tokens), 0) AS output_tokens
         FROM extraction_runs`,
      )
      .get();
    return { documents, invoices, exactDuplicates, possibleDuplicates, runs };
  }

  listExactDuplicates() {
    return this.db
      .prepare(
        `SELECT d.id AS document_id, d.file_name, d.sha256, o.file_path, o.first_seen_at
         FROM file_occurrences o JOIN documents d ON d.id = o.document_id
         WHERE o.document_id IN (SELECT document_id FROM file_occurrences GROUP BY document_id HAVING COUNT(*) > 1)
         ORDER BY d.id, o.id`,
      )
      .all();
  }

  listPossibleDuplicates() {
    return this.db
      .prepare(
        `SELECT i.business_key, i.id AS invoice_id, i.document_id, d.file_path, i.supplier_ruc, i.document_type,
           i.series, i.number, i.issue_date, i.currency, i.total_cents
         FROM invoices i JOIN documents d ON d.id = i.document_id
         WHERE i.business_key IN (SELECT business_key FROM invoices WHERE business_key IS NOT NULL
                                  GROUP BY business_key HAVING COUNT(*) > 1)
         ORDER BY i.business_key, i.id`,
      )
      .all();
  }

  /**
   * Facturas para exportar. Por defecto excluye las repeticiones de una misma
   * clave de negocio: conserva la leída de un XML si existe (datos exactos)
   * y, si no, la primera registrada.
   */
  listInvoicesForExport({
    month = null,
    validationStatuses,
    supplierRuc = null,
    currency = null,
    includeDuplicates = false,
  }) {
    const where = [];
    const params = [];
    if (month) {
      where.push('i.issue_date LIKE ?');
      params.push(`${month}-%`);
    }
    if (validationStatuses?.length) {
      where.push(`i.validation_status IN (${validationStatuses.map(() => '?').join(', ')})`);
      params.push(...validationStatuses);
    }
    if (supplierRuc) {
      where.push('i.supplier_ruc = ?');
      params.push(supplierRuc);
    }
    if (currency) {
      where.push('i.currency = ?');
      params.push(currency);
    }
    if (!includeDuplicates) {
      where.push(
        `(i.business_key IS NULL OR i.id = (SELECT i2.id FROM invoices i2 WHERE i2.business_key = i.business_key
           ORDER BY CASE i2.data_source WHEN 'xml' THEN 0 ELSE 1 END, i2.id LIMIT 1))`,
      );
    }
    const sql = `SELECT i.*, d.file_name, d.file_path, d.sha256,
        (SELECT COUNT(*) FROM invoices i3 WHERE i3.business_key = i.business_key AND i3.id <> i.id) AS duplicate_count
      FROM invoices i JOIN documents d ON d.id = i.document_id
      ${where.length ? `WHERE ${where.join(' AND ')}` : ''}
      ORDER BY i.issue_date, i.supplier_ruc, i.series, i.number, i.id`;
    return this.db.prepare(sql).all(...params);
  }

  /** Facturas con errores o advertencias sin resolver. */
  listInvoicesNeedingReview() {
    return this.db
      .prepare(
        `SELECT i.id AS invoice_id, i.document_id, i.validation_status, i.supplier_ruc, i.series, i.number,
           i.issue_date, i.currency, i.total_cents, d.file_path
         FROM invoices i JOIN documents d ON d.id = i.document_id
         WHERE i.validation_status IN ('error', 'warning')
         ORDER BY CASE i.validation_status WHEN 'error' THEN 0 ELSE 1 END, i.id`,
      )
      .all();
  }
}
