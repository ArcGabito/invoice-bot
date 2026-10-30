import fs from 'node:fs';
import path from 'node:path';
import { stringify } from 'csv-stringify/sync';
import { AMOUNT_FIELDS } from '../../domain/invoice.js';
import { formatCents } from '../../shared/money.js';

/**
 * Columnas del CSV contable. El orden y los nombres son parte del contrato
 * público (docs/csv-format.md): añade columnas al final, no las renombres.
 */
export const CSV_COLUMNS = Object.freeze([
  'invoice_id',
  'issue_date',
  'due_date',
  'supplier_ruc',
  'supplier_name',
  'customer_ruc',
  'customer_name',
  'document_type',
  'series',
  'number',
  'currency',
  ...AMOUNT_FIELDS,
  'validation_status',
  'duplicate_count',
  'file_name',
  'sha256',
  'data_source',
  'sunat_status',
]);

export function invoiceToCsvRecord(row) {
  const record = {};
  for (const column of CSV_COLUMNS) {
    if (column === 'invoice_id') record[column] = row.id;
    else if (AMOUNT_FIELDS.includes(column)) record[column] = formatCents(row[`${column}_cents`]);
    else record[column] = row[column] ?? '';
  }
  return record;
}

export function writeInvoicesCsv(rows, outputPath, { delimiter = ',', bom = false } = {}) {
  const csv = stringify(rows.map(invoiceToCsvRecord), {
    header: true,
    columns: CSV_COLUMNS,
    delimiter,
    bom,
    record_delimiter: 'windows',
  });
  fs.mkdirSync(path.dirname(outputPath), { recursive: true });
  fs.writeFileSync(outputPath, csv, 'utf8');
  return { path: outputPath, rows: rows.length };
}
