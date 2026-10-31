import fs from 'node:fs';
import path from 'node:path';
import { stringify } from 'csv-stringify/sync';
import { formatCents } from '../../shared/money.ts';
import { EXPORT_COLUMNS, columnValue } from './columns.ts';
import type { ExportRecord } from './columns.ts';

/** Claves de las columnas del CSV, en orden (contrato en docs/csv-format.md). */
export const CSV_COLUMNS = Object.freeze(EXPORT_COLUMNS.map((c) => c.key));

export function invoiceToCsvRecord(row: ExportRecord): Record<string, unknown> {
  const record: Record<string, unknown> = {};
  for (const column of EXPORT_COLUMNS) {
    const value = columnValue(row, column);
    record[column.key] = column.type === 'amount' ? formatCents(value as number | null) : (value ?? '');
  }
  return record;
}

export function writeInvoicesCsv(
  rows: readonly ExportRecord[],
  outputPath: string,
  { delimiter = ',', bom = false }: { delimiter?: string; bom?: boolean } = {},
): { path: string; rows: number } {
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
