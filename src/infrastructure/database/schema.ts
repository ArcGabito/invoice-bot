import type { Generated, Insertable, Selectable } from 'kysely';
import type { InvoiceData } from '../../domain/invoice.ts';

/**
 * Tipos de las tablas para Kysely. Las fechas llegan como texto ISO (las
 * normaliza NormalizeResultsPlugin): DATETIME2 → "2026-10-31T12:00:00.000Z"
 * y DATE → "2026-10-31". Los importes BIGINT llegan como número.
 * Las columnas con valor por defecto en la base de datos son Generated.
 */
type Timestamp = Generated<string>;

export interface DocumentsTable {
  id: Generated<number>;
  sha256: string;
  file_path: string;
  file_name: string;
  mime_type: string;
  file_size: number;
  status: Generated<string>;
  text_method: string | null;
  text_chars: number | null;
  page_count: number | null;
  attempts: Generated<number>;
  error_kind: string | null;
  error_message: string | null;
  skip_reason: string | null;
  linked_document_id: number | null;
  created_at: Timestamp;
  updated_at: Timestamp;
  processed_at: string | null;
}

export interface FileOccurrencesTable {
  id: Generated<number>;
  document_id: number;
  file_path: string;
  path_hash: Generated<Buffer>;
  first_seen_at: Timestamp;
  last_seen_at: Timestamp;
}

export interface ExtractionRunsTable {
  id: Generated<number>;
  document_id: number;
  attempt: number;
  provider: string;
  model: string | null;
  prompt_version: string;
  text_method: string | null;
  input_chars: number | null;
  status: 'success' | 'failed';
  error_kind: string | null;
  error_message: string | null;
  raw_response: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  duration_ms: number | null;
  created_at: Timestamp;
}

/** Columnas de datos del comprobante: las mismas que la factura normalizada del dominio. */
export type InvoiceDataColumns = InvoiceData;

export interface InvoicesTable extends InvoiceDataColumns {
  id: Generated<number>;
  document_id: number;
  extraction_run_id: number;
  data_source: Generated<string>;
  business_key: string | null;
  validation_status: string;
  rules_version: string;
  sunat_status: string | null;
  sunat_checked_at: string | null;
  review_status: Generated<string>;
  reviewed_at: string | null;
  reviewed_by: string | null;
  corrected: Generated<boolean>;
  created_at: Timestamp;
  updated_at: Timestamp;
}

export interface ValidationIssuesTable {
  id: Generated<number>;
  invoice_id: number;
  rule_code: string;
  severity: string;
  message: string;
  expected_value: string | null;
  actual_value: string | null;
  resolved_at: string | null;
  resolved_by: string | null;
  resolution_note: string | null;
}

export interface SunatChecksTable {
  id: Generated<number>;
  invoice_id: number;
  status: 'success' | 'failed';
  estado_cp: string | null;
  estado_ruc: string | null;
  cond_domi_ruc: string | null;
  observations: string | null;
  error_kind: string | null;
  error_message: string | null;
  raw_response: string | null;
  checked_at: Timestamp;
}

export interface ReviewActionsTable {
  id: Generated<number>;
  invoice_id: number;
  action: string;
  field: string | null;
  old_value: string | null;
  new_value: string | null;
  issue_id: number | null;
  note: string | null;
  user: string;
  created_at: Timestamp;
}

export interface BotRunsTable {
  id: Generated<number>;
  trigger: string;
  status: string;
  exit_code: number | null;
  summary: string | null;
  error_message: string | null;
  host: string | null;
  pid: number | null;
  started_at: Timestamp;
  finished_at: string | null;
}

export interface UsersTable {
  id: Generated<number>;
  username: string;
  display_name: string;
  password_hash: string;
  active: Generated<boolean>;
  created_at: Timestamp;
  updated_at: Timestamp;
  last_login_at: string | null;
}

export interface ReviewSessionsTable {
  token_hash: Buffer;
  user_id: number;
  user_version: string;
  csrf_token: string;
  ip: string | null;
  created_at: Timestamp;
  expires_at: string;
}

export interface SchemaMigrationsTable {
  version: number;
  name: string;
  applied_at: Timestamp;
}

export interface Database {
  documents: DocumentsTable;
  file_occurrences: FileOccurrencesTable;
  extraction_runs: ExtractionRunsTable;
  invoices: InvoicesTable;
  validation_issues: ValidationIssuesTable;
  sunat_checks: SunatChecksTable;
  review_actions: ReviewActionsTable;
  bot_runs: BotRunsTable;
  users: UsersTable;
  review_sessions: ReviewSessionsTable;
  schema_migrations: SchemaMigrationsTable;
}

export type DocumentRow = Selectable<DocumentsTable>;
export type InvoiceRow = Selectable<InvoicesTable>;
export type IssueRow = Selectable<ValidationIssuesTable>;
export type ExtractionRunRow = Selectable<ExtractionRunsTable>;
export type ReviewActionRow = Selectable<ReviewActionsTable>;
export type BotRunRow = Selectable<BotRunsTable>;
export type UserRow = Selectable<UsersTable>;
export type NewExtractionRun = Insertable<ExtractionRunsTable>;
