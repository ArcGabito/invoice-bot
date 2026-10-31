import { sql } from 'kysely';
import type { Kysely, Transaction } from 'kysely';
import { AMOUNT_FIELDS, DATA_SOURCE, DOCUMENT_STATUS } from '../../../domain/invoice.ts';
import type { Severity } from '../../../domain/invoice.ts';
import { statusFromIssues } from '../../../domain/invoice-validation.ts';
import { SUNAT_RULE_PREFIX } from '../../../domain/sunat-status.ts';
import type {
  BotRunRow,
  Database,
  DocumentRow,
  ExtractionRunRow,
  InvoiceDataColumns,
  InvoiceRow,
  IssueRow,
  NewExtractionRun,
  ReviewActionRow,
} from '../schema.ts';

/** Columnas de datos de la factura que se guardan, corrigen y exportan. */
export const INVOICE_COLUMNS = [
  'supplier_name',
  'supplier_ruc',
  'supplier_tax_id',
  'supplier_country',
  'customer_name',
  'customer_ruc',
  'document_type',
  'series',
  'number',
  'issue_date',
  'due_date',
  'currency',
  ...AMOUNT_FIELDS.map((f) => `${f}_cents` as const),
] as (keyof InvoiceDataColumns)[];

export interface NewIssue {
  rule_code: string;
  severity: Severity;
  message: string;
  expected_value?: string | null;
  actual_value?: string | null;
}

export type InvoiceData = Partial<InvoiceDataColumns>;

export interface InvoiceListItem {
  invoice_id: number;
  document_id: number;
  validation_status: string;
  review_status: string;
  supplier_name: string | null;
  supplier_ruc: string | null;
  document_type: string | null;
  series: string | null;
  number: string | null;
  issue_date: string | null;
  currency: string | null;
  total_cents: number | null;
  file_name: string;
  file_path?: string;
  reviewed_by?: string | null;
  reviewed_at?: string | null;
}

export type InvoiceDetail = InvoiceRow & {
  file_path: string;
  file_name: string;
  mime_type: string;
  sha256: string;
  page_count: number | null;
};

export type ExportRow = InvoiceRow & {
  file_name: string;
  file_path: string;
  sha256: string;
  duplicate_count: number;
};

export interface StatusSummary {
  documents: Record<string, number>;
  invoices: Record<string, number>;
  review: Record<string, number>;
  exactDuplicates: number;
  possibleDuplicates: number;
  runs: { total: number; failed: number; input_tokens: number; output_tokens: number };
}

export interface SunatCheckResult {
  estadoCp: string | null;
  estadoRuc: string | null;
  condDomiRuc: string | null;
  observaciones: string[];
  raw: unknown;
}

const now = () => new Date().toISOString();
const utcNow = sql<string>`SYSUTCDATETIME()`;

/** "2026-10" → ["2026-10-01", "2026-11-01"): rango que aprovecha el índice de issue_date. */
export function monthRange(month: string): [string, string] {
  const [y, m] = month.split('-').map(Number) as [number, number];
  const next = m === 12 ? `${y + 1}-01` : `${y}-${String(m + 1).padStart(2, '0')}`;
  return [`${month}-01`, `${next}-01`];
}

/** Una fecha que no existe (31 de febrero) no cabe en DATE: queda NULL y la validación ya la registró. */
function storableDate(value: string | null | undefined): string | null {
  if (!value || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
  const d = new Date(`${value}T00:00:00Z`);
  return !Number.isNaN(d.getTime()) && d.toISOString().slice(0, 10) === value ? value : null;
}

function invoiceValues(invoice: InvoiceData): InvoiceDataColumns {
  const row = Object.fromEntries(
    INVOICE_COLUMNS.map((c) => [c, invoice[c] ?? null]),
  ) as unknown as InvoiceDataColumns;
  row.issue_date = storableDate(row.issue_date);
  row.due_date = storableDate(row.due_date);
  return row;
}

const countBy = (rows: { key: string; n: number }[]) =>
  Object.fromEntries(rows.map((r) => [r.key, r.n])) as Record<string, number>;

/**
 * Implementación SQL Server del puerto InvoiceRepository. Dentro de
 * `transaction()` las operaciones usan la misma transacción.
 */
export class InvoiceRepository {
  readonly db: Kysely<Database> | Transaction<Database>;

  constructor(db: Kysely<Database> | Transaction<Database>) {
    this.db = db;
  }

  /** Ejecuta `fn` en una transacción: o se aplican todos los cambios o ninguno. */
  async transaction<T>(fn: (repo: InvoiceRepository) => Promise<T>): Promise<T> {
    if (this.db.isTransaction) return fn(this);
    return (this.db as Kysely<Database>)
      .transaction()
      .execute((trx) => fn(new InvoiceRepository(trx)));
  }

  // --- Documentos ---------------------------------------------------------

  async findDocumentBySha(sha256: string): Promise<DocumentRow | null> {
    return (
      (await this.db
        .selectFrom('documents')
        .selectAll()
        .where('sha256', '=', sha256)
        .executeTakeFirst()) ?? null
    );
  }

  async getDocument(id: number): Promise<DocumentRow | null> {
    return (
      (await this.db.selectFrom('documents').selectAll().where('id', '=', id).executeTakeFirst()) ??
      null
    );
  }

  /** Documento visto por última vez en `filePath` (según file_occurrences). */
  async findDocumentByPath(filePath: string): Promise<DocumentRow | null> {
    const result = await sql<DocumentRow>`
      SELECT d.* FROM file_occurrences o JOIN documents d ON d.id = o.document_id
      WHERE o.path_hash = HASHBYTES('SHA2_256', ${filePath}) AND o.file_path = ${filePath}`.execute(
      this.db,
    );
    return result.rows[0] ?? null;
  }

  async insertDocument(doc: {
    sha256: string;
    file_path: string;
    file_name: string;
    mime_type: string;
    file_size: number;
  }): Promise<DocumentRow> {
    return this.db
      .insertInto('documents')
      .values({ ...doc, status: DOCUMENT_STATUS.PENDING })
      .outputAll('inserted')
      .executeTakeFirstOrThrow();
  }

  /** Registra que el documento se vio en `filePath`. Devuelve true si la ruta es nueva para ese documento. */
  async recordOccurrence(documentId: number, filePath: string): Promise<boolean> {
    const existing = await sql<{ id: number; document_id: number }>`
      SELECT id, document_id FROM file_occurrences
      WHERE path_hash = HASHBYTES('SHA2_256', ${filePath}) AND file_path = ${filePath}`.execute(
      this.db,
    );
    const row = existing.rows[0];
    if (row) {
      // El archivo de esa ruta pudo cambiar de contenido: se reasigna.
      await this.db
        .updateTable('file_occurrences')
        .set({ document_id: documentId, last_seen_at: utcNow })
        .where('id', '=', row.id)
        .execute();
      return row.document_id !== documentId;
    }
    await this.db
      .insertInto('file_occurrences')
      .values({ document_id: documentId, file_path: filePath })
      .execute();
    return true;
  }

  async getOccurrences(documentId: number) {
    return this.db
      .selectFrom('file_occurrences')
      .select(['file_path', 'first_seen_at', 'last_seen_at'])
      .where('document_id', '=', documentId)
      .orderBy('id')
      .execute();
  }

  /** Marca el documento en proceso e incrementa su contador de intentos. */
  async startAttempt(documentId: number): Promise<number> {
    const row = await this.db
      .updateTable('documents')
      .set((eb) => ({
        status: DOCUMENT_STATUS.PROCESSING,
        attempts: eb('attempts', '+', 1),
        error_kind: null,
        error_message: null,
        updated_at: utcNow,
      }))
      .where('id', '=', documentId)
      .output('inserted.attempts')
      .executeTakeFirstOrThrow();
    return row.attempts;
  }

  async updateDocument(
    documentId: number,
    fields: Partial<
      Pick<
        DocumentRow,
        | 'status'
        | 'text_method'
        | 'text_chars'
        | 'page_count'
        | 'error_kind'
        | 'error_message'
        | 'skip_reason'
        | 'linked_document_id'
        | 'processed_at'
        | 'file_path'
        | 'file_name'
      >
    >,
  ): Promise<void> {
    const entries = Object.entries(fields).filter(([, v]) => v !== undefined);
    if (entries.length === 0) return;
    const values = Object.fromEntries(
      entries.map(([k, v]) => [
        k,
        k === 'error_message' && typeof v === 'string' ? v.slice(0, 4000) : v,
      ]),
    );
    await this.db
      .updateTable('documents')
      .set({ ...values, updated_at: utcNow })
      .where('id', '=', documentId)
      .execute();
  }

  /** Documentos que quedaron "en proceso" por una ejecución interrumpida. */
  async resetInterrupted(): Promise<number> {
    const result = await this.db
      .updateTable('documents')
      .set({ status: DOCUMENT_STATUS.PENDING, updated_at: utcNow })
      .where('status', '=', DOCUMENT_STATUS.PROCESSING)
      .executeTakeFirst();
    return Number(result.numUpdatedRows);
  }

  async listDocumentsByStatus(statuses: string[]): Promise<DocumentRow[]> {
    if (statuses.length === 0) return [];
    return this.db
      .selectFrom('documents')
      .selectAll()
      .where('status', 'in', statuses)
      .orderBy('id')
      .execute();
  }

  // --- Extracciones --------------------------------------------------------

  async insertExtractionRun(run: NewExtractionRun): Promise<number> {
    const row = await this.db
      .insertInto('extraction_runs')
      .values({ ...run, error_message: run.error_message?.slice(0, 4000) ?? null })
      .output('inserted.id')
      .executeTakeFirstOrThrow();
    return row.id;
  }

  async listExtractionRuns(documentId: number): Promise<ExtractionRunRow[]> {
    return this.db
      .selectFrom('extraction_runs')
      .selectAll()
      .where('document_id', '=', documentId)
      .orderBy('id')
      .execute();
  }

  // --- Facturas ------------------------------------------------------------

  /**
   * Guarda la factura, sus incidencias y el estado del documento en una sola
   * transacción. Si el documento ya tenía factura, se reemplaza.
   */
  async saveInvoiceResult({
    documentId,
    extractionRunId,
    invoice,
    validationStatus,
    rulesVersion,
    businessKey,
    dataSource = DATA_SOURCE.AI,
    issues,
  }: {
    documentId: number;
    extractionRunId: number;
    invoice: InvoiceData;
    validationStatus: string;
    rulesVersion: string;
    businessKey: string | null;
    dataSource?: string;
    issues: NewIssue[];
  }): Promise<number> {
    return this.transaction(async (repo) => {
      const values = invoiceValues(invoice);
      const existing = await repo.db
        .selectFrom('invoices')
        .select('id')
        .where('document_id', '=', documentId)
        .executeTakeFirst();
      let invoiceId: number;
      if (existing) {
        await repo.db
          .updateTable('invoices')
          .set({
            ...values,
            data_source: dataSource,
            extraction_run_id: extractionRunId,
            business_key: businessKey,
            validation_status: validationStatus,
            rules_version: rulesVersion,
            updated_at: utcNow,
            sunat_status: null,
            sunat_checked_at: null,
            review_status: 'pending',
            reviewed_at: null,
            reviewed_by: null,
            corrected: false,
          })
          .where('id', '=', existing.id)
          .execute();
        await repo.db
          .deleteFrom('validation_issues')
          .where('invoice_id', '=', existing.id)
          .execute();
        invoiceId = existing.id;
      } else {
        const row = await repo.db
          .insertInto('invoices')
          .values({
            ...values,
            document_id: documentId,
            extraction_run_id: extractionRunId,
            data_source: dataSource,
            business_key: businessKey,
            validation_status: validationStatus,
            rules_version: rulesVersion,
          })
          .output('inserted.id')
          .executeTakeFirstOrThrow();
        invoiceId = row.id;
      }
      await repo.insertIssues(invoiceId, issues);
      await repo.updateDocument(documentId, {
        status: DOCUMENT_STATUS.EXTRACTED,
        error_kind: null,
        error_message: null,
        processed_at: now(),
      });
      return invoiceId;
    });
  }

  async insertIssues(invoiceId: number, issues: NewIssue[]): Promise<void> {
    // Por lotes: SQL Server admite como máximo 2100 parámetros por consulta.
    for (let i = 0; i < issues.length; i += 200) {
      await this.db
        .insertInto('validation_issues')
        .values(
          issues.slice(i, i + 200).map((issue) => ({
            invoice_id: invoiceId,
            rule_code: issue.rule_code,
            severity: issue.severity,
            message: issue.message.slice(0, 1000),
            expected_value: issue.expected_value?.slice(0, 1000) ?? null,
            actual_value: issue.actual_value?.slice(0, 1000) ?? null,
          })),
        )
        .execute();
    }
  }

  async getInvoiceByDocument(documentId: number): Promise<InvoiceRow | null> {
    return (
      (await this.db
        .selectFrom('invoices')
        .selectAll()
        .where('document_id', '=', documentId)
        .executeTakeFirst()) ?? null
    );
  }

  async getIssues(invoiceId: number): Promise<IssueRow[]> {
    return this.db
      .selectFrom('validation_issues')
      .selectAll()
      .where('invoice_id', '=', invoiceId)
      .orderBy('id')
      .execute();
  }

  /**
   * Reserva la clave de negocio hasta que termine la transacción en curso:
   * dos documentos del mismo comprobante procesados a la vez (concurrencia,
   * o el bot y la pantalla de revisión) se ven el uno al otro como duplicados.
   * Debe llamarse dentro de `transaction()`, antes de `findByBusinessKey`.
   */
  async lockBusinessKey(businessKey: string): Promise<void> {
    if (!this.db.isTransaction) throw new Error('lockBusinessKey requiere una transacción');
    // sp_getapplock admite nombres de hasta 255 caracteres.
    const resource = `invoicebot:bk:${businessKey}`.slice(0, 255);
    const result = await sql<{ result: number }>`
      DECLARE @result INT;
      EXEC @result = sp_getapplock @Resource = ${resource}, @LockMode = 'Exclusive',
        @LockOwner = 'Transaction', @LockTimeout = 60000;
      SELECT @result AS result`.execute(this.db);
    if ((result.rows[0]?.result ?? -1) < 0) {
      throw new Error(`No se pudo reservar la clave de negocio ${businessKey}`);
    }
  }

  /** Otras facturas con la misma clave de negocio, de documentos distintos. */
  async findByBusinessKey(businessKey: string, excludeDocumentId: number) {
    return this.db
      .selectFrom('invoices as i')
      .innerJoin('documents as d', 'd.id', 'i.document_id')
      .select(['i.id', 'i.document_id', 'd.file_name', 'd.file_path'])
      .where('i.business_key', '=', businessKey)
      .where('i.document_id', '<>', excludeDocumentId)
      .where('i.review_status', '<>', 'rejected')
      .orderBy('i.id')
      .execute();
  }

  // --- Consultas -----------------------------------------------------------

  async getStatusSummary(): Promise<StatusSummary> {
    const documents = await sql<{ key: string; n: number }>`
      SELECT status AS [key], COUNT(*) AS n FROM documents GROUP BY status`.execute(this.db);
    const invoices = await sql<{ key: string; n: number }>`
      SELECT validation_status AS [key], COUNT(*) AS n FROM invoices GROUP BY validation_status`.execute(
      this.db,
    );
    const review = await sql<{ key: string; n: number }>`
      SELECT review_status AS [key], COUNT(*) AS n FROM invoices GROUP BY review_status`.execute(
      this.db,
    );
    const dup = await sql<{ exact: number; possible: number }>`
      SELECT
        (SELECT COUNT(*) FROM (SELECT document_id FROM file_occurrences GROUP BY document_id HAVING COUNT(*) > 1) AS e) AS exact,
        (SELECT COUNT(*) FROM (SELECT business_key FROM invoices
           WHERE business_key IS NOT NULL AND review_status <> N'rejected'
           GROUP BY business_key HAVING COUNT(*) > 1) AS p) AS possible`.execute(this.db);
    const runs = await sql<StatusSummary['runs']>`
      SELECT COUNT(*) AS total,
        COALESCE(SUM(CASE WHEN status = N'failed' THEN 1 ELSE 0 END), 0) AS failed,
        COALESCE(SUM(CAST(input_tokens AS BIGINT)), 0) AS input_tokens,
        COALESCE(SUM(CAST(output_tokens AS BIGINT)), 0) AS output_tokens
      FROM extraction_runs`.execute(this.db);
    const r = runs.rows[0] as unknown as Record<string, number>;
    return {
      documents: countBy(documents.rows),
      invoices: countBy(invoices.rows),
      review: countBy(review.rows),
      exactDuplicates: dup.rows[0]?.exact ?? 0,
      possibleDuplicates: dup.rows[0]?.possible ?? 0,
      runs: {
        total: Number(r.total ?? 0),
        failed: Number(r.failed ?? 0),
        input_tokens: Number(r.input_tokens ?? 0),
        output_tokens: Number(r.output_tokens ?? 0),
      },
    };
  }

  async listExactDuplicates() {
    const result = await sql<{
      document_id: number;
      file_name: string;
      sha256: string;
      file_path: string;
      first_seen_at: string;
    }>`
      SELECT d.id AS document_id, d.file_name, d.sha256, o.file_path, o.first_seen_at
      FROM file_occurrences o JOIN documents d ON d.id = o.document_id
      WHERE o.document_id IN (SELECT document_id FROM file_occurrences GROUP BY document_id HAVING COUNT(*) > 1)
      ORDER BY d.id, o.id`.execute(this.db);
    return result.rows;
  }

  async listPossibleDuplicates() {
    const result = await sql<{
      business_key: string;
      invoice_id: number;
      document_id: number;
      file_path: string;
      supplier_ruc: string | null;
      document_type: string | null;
      series: string | null;
      number: string | null;
      issue_date: string | null;
      currency: string | null;
      total_cents: number | null;
    }>`
      SELECT i.business_key, i.id AS invoice_id, i.document_id, d.file_path, i.supplier_ruc, i.document_type,
        i.series, i.number, i.issue_date, i.currency, i.total_cents
      FROM invoices i JOIN documents d ON d.id = i.document_id
      WHERE i.review_status <> N'rejected'
        AND i.business_key IN (SELECT business_key FROM invoices
                               WHERE business_key IS NOT NULL AND review_status <> N'rejected'
                               GROUP BY business_key HAVING COUNT(*) > 1)
      ORDER BY i.business_key, i.id`.execute(this.db);
    return result.rows;
  }

  /**
   * Facturas para exportar. Por defecto excluye las repeticiones de una misma
   * clave de negocio: conserva la leída de un XML si existe (datos exactos)
   * y, si no, la del primer documento registrado (no la primera en terminar
   * de procesarse, que depende de la concurrencia).
   */
  async listInvoicesForExport({
    month = null,
    validationStatuses = [],
    supplierRuc = null,
    currency = null,
    includeDuplicates = false,
  }: {
    month?: string | null;
    validationStatuses?: string[];
    supplierRuc?: string | null;
    currency?: string | null;
    includeDuplicates?: boolean;
  }): Promise<ExportRow[]> {
    const where = [sql`i.review_status <> N'rejected'`];
    if (month) {
      const [from, to] = monthRange(month);
      where.push(sql`i.issue_date >= ${from} AND i.issue_date < ${to}`);
    }
    if (validationStatuses.length)
      where.push(sql`i.validation_status IN (${sql.join(validationStatuses)})`);
    if (supplierRuc) where.push(sql`i.supplier_ruc = ${supplierRuc}`);
    if (currency) where.push(sql`i.currency = ${currency}`);
    if (!includeDuplicates) {
      where.push(sql`(i.business_key IS NULL OR i.id = (SELECT TOP (1) i2.id FROM invoices i2
        WHERE i2.business_key = i.business_key AND i2.review_status <> N'rejected'
        ORDER BY CASE i2.data_source WHEN N'xml' THEN 0 ELSE 1 END, i2.document_id))`);
    }
    const result = await sql<ExportRow>`
      SELECT i.*, d.file_name, d.file_path, d.sha256,
        (SELECT COUNT(*) FROM invoices i3 WHERE i3.business_key = i.business_key AND i3.id <> i.id) AS duplicate_count
      FROM invoices i JOIN documents d ON d.id = i.document_id
      WHERE ${sql.join(where, sql` AND `)}
      ORDER BY i.issue_date, i.supplier_ruc, i.series, i.number, i.id`.execute(this.db);
    return result.rows;
  }

  /** Facturas con errores o advertencias sin rechazar. */
  async listInvoicesNeedingReview(): Promise<InvoiceListItem[]> {
    const result = await sql<InvoiceListItem>`
      SELECT i.id AS invoice_id, i.document_id, i.validation_status, i.review_status, i.supplier_name,
        i.supplier_ruc, i.document_type, i.series, i.number, i.issue_date, i.currency, i.total_cents,
        d.file_path, d.file_name
      FROM invoices i JOIN documents d ON d.id = i.document_id
      WHERE i.validation_status IN (N'error', N'warning') AND i.review_status <> N'rejected'
      ORDER BY CASE i.validation_status WHEN N'error' THEN 0 ELSE 1 END, i.id`.execute(this.db);
    return result.rows;
  }

  // --- Revisión humana -----------------------------------------------------

  /** Factura con los datos de su documento original, o null. */
  async getInvoiceDetail(invoiceId: number): Promise<InvoiceDetail | null> {
    const result = await sql<InvoiceDetail>`
      SELECT i.*, d.file_path, d.file_name, d.mime_type, d.sha256, d.page_count
      FROM invoices i JOIN documents d ON d.id = i.document_id WHERE i.id = ${invoiceId}`.execute(
      this.db,
    );
    return result.rows[0] ?? null;
  }

  async listInvoicesByReviewStatus(status: string, limit = 200): Promise<InvoiceListItem[]> {
    const result = await sql<InvoiceListItem>`
      SELECT TOP (${limit}) i.id AS invoice_id, i.document_id, i.validation_status, i.review_status, i.supplier_name,
        i.supplier_ruc, i.document_type, i.series, i.number, i.issue_date, i.currency, i.total_cents,
        i.reviewed_by, i.reviewed_at, d.file_name
      FROM invoices i JOIN documents d ON d.id = i.document_id
      WHERE i.review_status = ${status} ORDER BY i.reviewed_at DESC, i.id DESC`.execute(this.db);
    return result.rows;
  }

  async listReviewActions(invoiceId: number): Promise<ReviewActionRow[]> {
    return this.db
      .selectFrom('review_actions')
      .selectAll()
      .where('invoice_id', '=', invoiceId)
      .orderBy('id')
      .execute();
  }

  async recordReviewAction({
    invoiceId,
    action,
    user,
    field = null,
    oldValue = null,
    newValue = null,
    issueId = null,
    note = null,
  }: {
    invoiceId: number;
    action: string;
    user: string;
    field?: string | null;
    oldValue?: unknown;
    newValue?: unknown;
    issueId?: number | null;
    note?: string | null;
  }): Promise<void> {
    await this.db
      .insertInto('review_actions')
      .values({
        invoice_id: invoiceId,
        action,
        field,
        old_value:
          oldValue === null || oldValue === undefined ? null : String(oldValue).slice(0, 1000),
        new_value:
          newValue === null || newValue === undefined ? null : String(newValue).slice(0, 1000),
        issue_id: issueId,
        note: note?.slice(0, 1000) ?? null,
        user,
      })
      .execute();
  }

  async setReviewStatus(invoiceId: number, status: string, user: string): Promise<void> {
    const pending = status === 'pending';
    await this.db
      .updateTable('invoices')
      .set({
        review_status: status,
        reviewed_at: pending ? null : utcNow,
        reviewed_by: pending ? null : user,
        updated_at: utcNow,
      })
      .where('id', '=', invoiceId)
      .execute();
  }

  /** Marca como resueltas las incidencias abiertas de las severidades indicadas. */
  async resolveOpenIssues(
    invoiceId: number,
    { severities, user, note = null }: { severities: string[]; user: string; note?: string | null },
  ): Promise<number> {
    const result = await this.db
      .updateTable('validation_issues')
      .set({ resolved_at: utcNow, resolved_by: user, resolution_note: note })
      .where('invoice_id', '=', invoiceId)
      .where('resolved_at', 'is', null)
      .where('severity', 'in', severities)
      .executeTakeFirst();
    return Number(result.numUpdatedRows);
  }

  async getIssue(issueId: number): Promise<IssueRow | null> {
    return (
      (await this.db
        .selectFrom('validation_issues')
        .selectAll()
        .where('id', '=', issueId)
        .executeTakeFirst()) ?? null
    );
  }

  async resolveIssue(
    issueId: number,
    { user, note = null }: { user: string; note?: string | null },
  ): Promise<void> {
    await this.db
      .updateTable('validation_issues')
      .set({ resolved_at: utcNow, resolved_by: user, resolution_note: note })
      .where('id', '=', issueId)
      .execute();
  }

  /** Vuelve a abrir todas las incidencias resueltas de una factura. */
  async reopenIssues(invoiceId: number): Promise<number> {
    const result = await this.db
      .updateTable('validation_issues')
      .set({ resolved_at: null, resolved_by: null, resolution_note: null })
      .where('invoice_id', '=', invoiceId)
      .where('resolved_at', 'is not', null)
      .executeTakeFirst();
    return Number(result.numUpdatedRows);
  }

  /** Aplica correcciones manuales ({ columna: valor }) y marca la factura como corregida. */
  async updateInvoiceFields(invoiceId: number, changes: InvoiceData): Promise<void> {
    const entries = Object.entries(changes).filter(([c]) =>
      (INVOICE_COLUMNS as string[]).includes(c),
    );
    if (entries.length === 0) return;
    const values = Object.fromEntries(entries) as InvoiceData;
    if ('issue_date' in values) values.issue_date = storableDate(values.issue_date);
    if ('due_date' in values) values.due_date = storableDate(values.due_date);
    await this.db
      .updateTable('invoices')
      .set({ ...values, corrected: true, updated_at: utcNow })
      .where('id', '=', invoiceId)
      .execute();
  }

  /** La consulta a SUNAT deja de valer si cambian los datos consultados: se repetirá. */
  async clearSunatStatus(invoiceId: number): Promise<void> {
    await this.db
      .updateTable('invoices')
      .set({ sunat_status: null, sunat_checked_at: null })
      .where('id', '=', invoiceId)
      .execute();
  }

  async setBusinessKey(invoiceId: number, businessKey: string | null): Promise<void> {
    await this.db
      .updateTable('invoices')
      .set({ business_key: businessKey })
      .where('id', '=', invoiceId)
      .execute();
  }

  /**
   * Reemplaza las incidencias abiertas que salen de las reglas de validación;
   * las que `isPreserved` indique (SUNAT, duplicados, notas) se mantienen.
   */
  async replaceRuleIssues(
    invoiceId: number,
    issues: NewIssue[],
    isPreserved: (ruleCode: string) => boolean,
  ): Promise<void> {
    const open = await this.db
      .selectFrom('validation_issues')
      .select(['id', 'rule_code'])
      .where('invoice_id', '=', invoiceId)
      .where('resolved_at', 'is', null)
      .execute();
    const remove = open.filter((r) => !isPreserved(r.rule_code)).map((r) => r.id);
    if (remove.length)
      await this.db.deleteFrom('validation_issues').where('id', 'in', remove).execute();
    await this.insertIssues(invoiceId, issues);
  }

  /** Recalcula el estado de validación con las incidencias abiertas. */
  async recomputeValidationStatus(invoiceId: number): Promise<string> {
    const open = await this.db
      .selectFrom('validation_issues')
      .select('severity')
      .where('invoice_id', '=', invoiceId)
      .where('resolved_at', 'is', null)
      .execute();
    const status = statusFromIssues(open as { severity: Severity }[]);
    await this.db
      .updateTable('invoices')
      .set({ validation_status: status, updated_at: utcNow })
      .where('id', '=', invoiceId)
      .execute();
    return status;
  }

  // --- Ejecuciones desatendidas --------------------------------------------

  async startBotRun({
    trigger,
    host,
    pid,
  }: {
    trigger: string;
    host: string | null;
    pid: number | null;
  }): Promise<number> {
    const row = await this.db
      .insertInto('bot_runs')
      .values({ trigger, status: 'running', host, pid })
      .output('inserted.id')
      .executeTakeFirstOrThrow();
    return row.id;
  }

  async finishBotRun(
    runId: number,
    {
      status,
      exitCode,
      summary = null,
      errorMessage = null,
    }: { status: string; exitCode: number; summary?: unknown; errorMessage?: string | null },
  ): Promise<void> {
    await this.db
      .updateTable('bot_runs')
      .set({
        status,
        exit_code: exitCode,
        summary: summary ? JSON.stringify(summary) : null,
        error_message: errorMessage?.slice(0, 4000) ?? null,
        finished_at: utcNow,
      })
      .where('id', '=', runId)
      .execute();
  }

  /** Ejecuciones que quedaron "running" porque el proceso murió sin cerrarlas. */
  async closeAbandonedBotRuns(): Promise<number> {
    const result = await this.db
      .updateTable('bot_runs')
      .set({
        status: 'failed',
        error_message: 'La ejecución terminó de forma inesperada',
        finished_at: utcNow,
      })
      .where('status', '=', 'running')
      .executeTakeFirst();
    return Number(result.numUpdatedRows);
  }

  async listBotRuns(limit = 10): Promise<(Omit<BotRunRow, 'summary'> & { summary: unknown })[]> {
    const rows = await this.db
      .selectFrom('bot_runs')
      .selectAll()
      .orderBy('id', 'desc')
      .top(limit)
      .execute();
    return rows.map((r) => ({ ...r, summary: r.summary ? JSON.parse(r.summary) : null }));
  }

  // --- SUNAT ---------------------------------------------------------------

  /**
   * Facturas con los datos necesarios para consultarlas en SUNAT. Sin `force`,
   * solo las nunca consultadas con éxito y las que SUNAT aún no tenía
   * informadas (una boleta puede tardar días en aparecer).
   */
  async listInvoicesForSunatCheck({
    month = null,
    documentTypes,
    force = false,
    limit = null,
  }: {
    month?: string | null;
    documentTypes: string[];
    force?: boolean;
    limit?: number | null;
  }): Promise<InvoiceRow[]> {
    let query = this.db
      .selectFrom('invoices')
      .selectAll()
      .where('supplier_ruc', 'is not', null)
      .where('series', 'is not', null)
      .where('number', 'is not', null)
      .where('issue_date', 'is not', null)
      .where('document_type', 'in', documentTypes);
    if (month) {
      const [from, to] = monthRange(month);
      query = query.where('issue_date', '>=', from).where('issue_date', '<', to);
    }
    if (!force) {
      query = query.where((eb) =>
        eb.or([eb('sunat_checked_at', 'is', null), eb('sunat_status', '=', 'NO EXISTE')]),
      );
    }
    query = query.orderBy('issue_date').orderBy('id');
    if (limit) query = query.top(Number(limit));
    return query.execute();
  }

  /**
   * Registra una consulta a SUNAT. Si fue correcta, reemplaza las incidencias
   * SUNAT_* anteriores por las nuevas, guarda el estado del comprobante y
   * recalcula el estado de validación con todas las incidencias abiertas.
   */
  async recordSunatCheck({
    invoiceId,
    result = null,
    error = null,
    receiptStatus = null,
    issues = [],
  }: {
    invoiceId: number;
    result?: SunatCheckResult | null;
    error?: { kind?: string; message: string } | null;
    receiptStatus?: string | null;
    issues?: NewIssue[];
  }): Promise<string | null> {
    return this.transaction(async (repo) => {
      await repo.db
        .insertInto('sunat_checks')
        .values({
          invoice_id: invoiceId,
          status: error ? 'failed' : 'success',
          estado_cp: result?.estadoCp ?? null,
          estado_ruc: result?.estadoRuc ?? null,
          cond_domi_ruc: result?.condDomiRuc ?? null,
          observations: result ? JSON.stringify(result.observaciones) : null,
          error_kind: error?.kind ?? null,
          error_message: error?.message.slice(0, 4000) ?? null,
          raw_response: result ? JSON.stringify(result.raw) : null,
        })
        .execute();
      if (error) return null;

      await sql`DELETE FROM validation_issues
        WHERE invoice_id = ${invoiceId} AND LEFT(rule_code, ${SUNAT_RULE_PREFIX.length}) = ${SUNAT_RULE_PREFIX}`.execute(
        repo.db,
      );
      await repo.insertIssues(invoiceId, issues);
      const open = await repo.db
        .selectFrom('validation_issues')
        .select('severity')
        .where('invoice_id', '=', invoiceId)
        .where('resolved_at', 'is', null)
        .execute();
      const validationStatus = statusFromIssues(open as { severity: Severity }[]);
      await repo.db
        .updateTable('invoices')
        .set({
          sunat_status: receiptStatus,
          sunat_checked_at: utcNow,
          validation_status: validationStatus,
          updated_at: utcNow,
        })
        .where('id', '=', invoiceId)
        .execute();
      return validationStatus;
    });
  }
}
