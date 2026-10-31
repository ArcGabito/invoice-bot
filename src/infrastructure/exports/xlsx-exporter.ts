import fs from 'node:fs';
import path from 'node:path';
import writeExcelFile from 'write-excel-file/node';
import { EXPORT_COLUMNS, columnValue } from './columns.ts';
import type { ExportColumn, ExportRecord } from './columns.ts';

const AMOUNT_FORMAT = '#,##0.00';
const DATE_FORMAT = 'dd/mm/yyyy';
const HEADER_STYLE = { fontWeight: 'bold', backgroundColor: '#DDEBF7', wrap: true } as const;

/** "2026-10-15" → fecha de Excel. Un texto que no es fecha se deja como texto. */
function toDate(value: unknown): Date | unknown {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(typeof value === 'string' ? value : '');
  return m ? new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]))) : value;
}

function cellFor(column: ExportColumn, row: ExportRecord) {
  const value = columnValue(row, column);
  if (value === null || value === '') return null;
  switch (column.type) {
    case 'amount':
      // Importes como números reales, para que Excel pueda sumarlos.
      return { value: Number(value) / 100, type: Number, format: AMOUNT_FORMAT };
    case 'date': {
      const date = toDate(value);
      return date instanceof Date ? { value: date, type: Date, format: DATE_FORMAT } : { value: String(date) };
    }
    case 'id':
    case 'int':
      return { value: Number(value), type: Number };
    default:
      // RUC, serie y número se guardan como texto: no deben perder ceros.
      return { value: String(value), type: String };
  }
}

/** Totales por moneda y estado de validación, para la hoja "Resumen". */
interface SummaryGroup {
  currency: string;
  status: string;
  count: number;
  taxable: number;
  igv: number;
  total: number;
}

function summarize(rows: readonly ExportRecord[]): SummaryGroup[] {
  const groups = new Map<string, SummaryGroup>();
  for (const row of rows) {
    const currency = (row.currency as string | null) ?? '—';
    const status = String(row.validation_status);
    const key = `${currency}|${status}`;
    const g = groups.get(key) ?? {
      currency,
      status,
      count: 0,
      taxable: 0,
      igv: 0,
      total: 0,
    };
    g.count++;
    g.taxable += (row.taxable_amount_cents as number | null) ?? 0;
    g.igv += (row.igv_cents as number | null) ?? 0;
    g.total += (row.total_cents as number | null) ?? 0;
    groups.set(key, g);
  }
  return [...groups.values()].sort((a, b) => `${a.currency}${a.status}`.localeCompare(`${b.currency}${b.status}`));
}

/**
 * Escribe las facturas en un Excel con dos hojas: "Facturas" (una fila por
 * comprobante, importes numéricos y fechas reales) y "Resumen" (totales por
 * moneda y estado de validación).
 */
export async function writeInvoicesXlsx(
  rows: readonly ExportRecord[],
  outputPath: string,
  { title = null }: { title?: string | null } = {},
): Promise<{ path: string; rows: number }> {
  const invoiceSheet = {
    sheet: 'Facturas',
    data: [
      EXPORT_COLUMNS.map((c) => ({ value: c.label, ...HEADER_STYLE })),
      ...rows.map((row) => EXPORT_COLUMNS.map((c) => cellFor(c, row))),
    ],
    columns: EXPORT_COLUMNS.map((c) => ({ width: c.width ?? (c.type === 'amount' ? 12 : 14) })),
    stickyRowsCount: 1,
  };

  const amount = (cents: number) => ({ value: cents / 100, type: Number, format: AMOUNT_FORMAT });
  const summaryRows = summarize(rows).map((g) => [
    { value: g.currency },
    { value: g.status },
    { value: g.count, type: Number },
    amount(g.taxable),
    amount(g.igv),
    amount(g.total),
  ]);
  const summarySheet = {
    sheet: 'Resumen',
    data: [
      ...(title ? [[{ value: title, fontWeight: 'bold' as const }], []] : []),
      ['Moneda', 'Validación', 'Comprobantes', 'Op. gravadas', 'IGV', 'Total'].map((v) => ({
        value: v,
        ...HEADER_STYLE,
      })),
      ...summaryRows,
    ],
    columns: [{ width: 10 }, { width: 12 }, { width: 14 }, { width: 14 }, { width: 14 }, { width: 14 }],
  };

  fs.mkdirSync(path.dirname(outputPath), { recursive: true });
  await writeExcelFile([invoiceSheet, summarySheet]).toFile(outputPath);
  return { path: outputPath, rows: rows.length };
}
