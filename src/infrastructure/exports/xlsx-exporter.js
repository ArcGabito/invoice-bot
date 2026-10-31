import fs from 'node:fs';
import path from 'node:path';
import writeExcelFile from 'write-excel-file/node';
import { EXPORT_COLUMNS, columnValue } from './columns.js';

const AMOUNT_FORMAT = '#,##0.00';
const DATE_FORMAT = 'dd/mm/yyyy';
const HEADER_STYLE = { fontWeight: 'bold', backgroundColor: '#DDEBF7', wrap: true };

/** "2026-10-15" → fecha de Excel. Un texto que no es fecha se deja como texto. */
function toDate(value) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value ?? '');
  return m ? new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]))) : value;
}

function cellFor(column, row) {
  const value = columnValue(row, column);
  if (value === null || value === '') return null;
  switch (column.type) {
    case 'amount':
      // Importes como números reales, para que Excel pueda sumarlos.
      return { value: value / 100, type: Number, format: AMOUNT_FORMAT };
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
function summarize(rows) {
  const groups = new Map();
  for (const row of rows) {
    const key = `${row.currency ?? '—'}|${row.validation_status}`;
    const g = groups.get(key) ?? {
      currency: row.currency ?? '—',
      status: row.validation_status,
      count: 0,
      taxable: 0,
      igv: 0,
      total: 0,
    };
    g.count++;
    g.taxable += row.taxable_amount_cents ?? 0;
    g.igv += row.igv_cents ?? 0;
    g.total += row.total_cents ?? 0;
    groups.set(key, g);
  }
  return [...groups.values()].sort((a, b) => `${a.currency}${a.status}`.localeCompare(`${b.currency}${b.status}`));
}

/**
 * Escribe las facturas en un Excel con dos hojas: "Facturas" (una fila por
 * comprobante, importes numéricos y fechas reales) y "Resumen" (totales por
 * moneda y estado de validación).
 */
export async function writeInvoicesXlsx(rows, outputPath, { title = null } = {}) {
  const invoiceSheet = {
    sheet: 'Facturas',
    data: [
      EXPORT_COLUMNS.map((c) => ({ value: c.label, ...HEADER_STYLE })),
      ...rows.map((row) => EXPORT_COLUMNS.map((c) => cellFor(c, row))),
    ],
    columns: EXPORT_COLUMNS.map((c) => ({ width: c.width ?? (c.type === 'amount' ? 12 : 14) })),
    stickyRowsCount: 1,
  };

  const amount = (cents) => ({ value: cents / 100, type: Number, format: AMOUNT_FORMAT });
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
      ...(title ? [[{ value: title, fontWeight: 'bold' }], []] : []),
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
