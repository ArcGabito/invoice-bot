import fs from 'node:fs';
import path from 'node:path';
import { stringify } from 'csv-stringify/sync';
import { formatCents } from '../../shared/money.js';
import { EXPORT_COLUMNS, columnValue } from './columns.js';

/** Claves de las columnas del CSV, en orden (contrato en docs/csv-format.md). */
export const CSV_COLUMNS = Object.freeze(EXPORT_COLUMNS.map((c) => c.key));

export function invoiceToCsvRecord(row) {
  const record = {};
  for (const column of EXPORT_COLUMNS) {
    const value = columnValue(row, column);
    record[column.key] = column.type === 'amount' ? formatCents(value) : (value ?? '');
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
