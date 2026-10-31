import { formatCents, toCents } from '../shared/money.ts';
import { AMOUNT_FIELDS, normalizeInvoice } from './invoice.ts';
import type { AmountField, InvoiceData } from './invoice.ts';

export const REVIEW_STATUS = Object.freeze({
  PENDING: 'pending',
  APPROVED: 'approved',
  REJECTED: 'rejected',
} as const);

export type ReviewStatus = (typeof REVIEW_STATUS)[keyof typeof REVIEW_STATUS];

/** Campos de texto que una persona puede corregir (los importes se añaden abajo). */
const TEXT_FIELDS: readonly string[] = [
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
];

export const CORRECTABLE_FIELDS: readonly string[] = Object.freeze([
  ...TEXT_FIELDS,
  ...AMOUNT_FIELDS,
]);

export class CorrectionError extends Error {}

/**
 * Convierte una corrección escrita por una persona ("total=1,180.50",
 * "issue_date=15/10/2026", "supplier_ruc=") en el valor que se guarda,
 * aplicando la misma normalización que a los datos extraídos. Un valor vacío
 * borra el dato.
 */
export interface Correction {
  field: string;
  column: keyof InvoiceData;
  value: string | number | null;
  display: string;
}

export function parseCorrection(field: string, raw: unknown): Correction {
  if (!CORRECTABLE_FIELDS.includes(field)) {
    throw new CorrectionError(
      `No se puede corregir "${field}". Campos permitidos: ${CORRECTABLE_FIELDS.join(', ')}`,
    );
  }
  const text = raw === null || raw === undefined ? '' : String(raw).trim();
  if ((AMOUNT_FIELDS as readonly string[]).includes(field)) {
    let cents: number | null;
    try {
      cents = text === '' ? null : toCents(text);
    } catch {
      throw new CorrectionError(`Importe no válido para ${field}: "${raw}"`);
    }
    return {
      field,
      column: `${field as AmountField}_cents`,
      value: cents,
      display: cents === null ? '' : formatCents(cents),
    };
  }
  // Se reutiliza la normalización de la extracción: misma regla para la IA y para las personas.
  const { invoice } = normalizeInvoice({ [field]: text === '' ? null : text });
  const column = field as keyof InvoiceData;
  const value = invoice[column] ?? null;
  return { field, column, value, display: value === null ? '' : String(value) };
}

/** "total=118.00" → ["total", "118.00"]. El valor puede contener "=". */
export function splitAssignment(assignment: string): [string, string] {
  const index = assignment.indexOf('=');
  if (index <= 0) throw new CorrectionError(`Usa campo=valor (recibido: "${assignment}")`);
  return [assignment.slice(0, index).trim(), assignment.slice(index + 1)];
}

/**
 * Incidencias que no salen de las reglas de validación y que, por tanto, se
 * conservan cuando se revalida una factura corregida: las de SUNAT y las notas
 * del modelo o del XML. Los posibles duplicados sí se recalculan, porque la
 * corrección puede cambiar el emisor, la serie o el número.
 */
export function isPreservedOnRevalidation(ruleCode: string): boolean {
  return (
    ruleCode.startsWith('SUNAT_') ||
    ['MODEL_NOTE', 'XML_NOTE', 'MODEL_DOUBTED_INVOICE'].includes(ruleCode)
  );
}

/** Corregir uno de estos datos invalida la última consulta a SUNAT. */
export const SUNAT_RELEVANT_FIELDS = Object.freeze([
  'supplier_ruc',
  'document_type',
  'series',
  'number',
  'issue_date',
  'total',
]);
