/**
 * Columnas de exportación, compartidas por CSV y Excel. El orden y las claves
 * son parte del contrato público (docs/csv-format.md): las columnas nuevas se
 * añaden al final y las existentes no se renombran.
 *
 * - key: nombre de la columna en el CSV.
 * - label: encabezado en Excel, pensado para personas.
 * - type: id | text | date | amount | int.
 */
export type ColumnType = 'id' | 'text' | 'date' | 'amount' | 'int';

export interface ExportColumn {
  key: string;
  label: string;
  type: ColumnType;
  width?: number;
}

/** Fila de la consulta de exportación (ExportRow del repositorio). */
export type ExportRecord = Record<string, unknown>;

export const EXPORT_COLUMNS: readonly ExportColumn[] = Object.freeze([
  { key: 'invoice_id', label: 'ID', type: 'id', width: 7 },
  { key: 'issue_date', label: 'Fecha de emisión', type: 'date', width: 12 },
  { key: 'due_date', label: 'Vencimiento', type: 'date', width: 12 },
  { key: 'supplier_ruc', label: 'RUC emisor', type: 'text', width: 13 },
  { key: 'supplier_name', label: 'Emisor', type: 'text', width: 34 },
  { key: 'customer_ruc', label: 'RUC/DNI cliente', type: 'text', width: 13 },
  { key: 'customer_name', label: 'Cliente', type: 'text', width: 28 },
  { key: 'document_type', label: 'Tipo', type: 'text', width: 6 },
  { key: 'series', label: 'Serie', type: 'text', width: 7 },
  { key: 'number', label: 'Número', type: 'text', width: 11 },
  { key: 'currency', label: 'Moneda', type: 'text', width: 8 },
  { key: 'subtotal', label: 'Subtotal', type: 'amount' },
  { key: 'taxable_amount', label: 'Op. gravadas', type: 'amount' },
  { key: 'exempt_amount', label: 'Op. exoneradas', type: 'amount' },
  { key: 'unaffected_amount', label: 'Op. inafectas', type: 'amount' },
  { key: 'discount_amount', label: 'Descuentos', type: 'amount' },
  { key: 'igv', label: 'IGV', type: 'amount' },
  { key: 'isc', label: 'ISC', type: 'amount' },
  { key: 'icbper', label: 'ICBPER', type: 'amount' },
  { key: 'other_charges', label: 'Otros cargos', type: 'amount' },
  { key: 'other_taxes', label: 'Otros tributos', type: 'amount' },
  { key: 'total', label: 'Total', type: 'amount' },
  { key: 'validation_status', label: 'Validación', type: 'text', width: 11 },
  { key: 'duplicate_count', label: 'Duplicados', type: 'int', width: 10 },
  { key: 'file_name', label: 'Archivo', type: 'text', width: 34 },
  { key: 'sha256', label: 'SHA-256', type: 'text', width: 20 },
  { key: 'data_source', label: 'Origen', type: 'text', width: 8 },
  { key: 'sunat_status', label: 'Estado SUNAT', type: 'text', width: 14 },
  { key: 'prepaid_amount', label: 'Anticipos', type: 'amount' },
  { key: 'supplier_tax_id', label: 'ID fiscal emisor (exterior)', type: 'text', width: 16 },
  { key: 'supplier_country', label: 'País emisor', type: 'text', width: 8 },
  { key: 'review_status', label: 'Revisión', type: 'text', width: 10 },
]);

/** Valor de una columna para una fila de la consulta de exportación. */
export function columnValue(row: ExportRecord, column: ExportColumn): unknown {
  if (column.key === 'invoice_id') return row.id;
  if (column.type === 'amount') return row[`${column.key}_cents`] ?? null;
  return row[column.key] ?? null;
}
