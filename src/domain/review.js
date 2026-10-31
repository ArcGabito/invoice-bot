import { formatCents, toCents } from '../shared/money.js';
import { AMOUNT_FIELDS, normalizeInvoice } from './invoice.js';

export const REVIEW_STATUS = Object.freeze({
  PENDING: 'pending',
  APPROVED: 'approved',
  REJECTED: 'rejected',
});

/** Campos de texto que una persona puede corregir (los importes se añaden abajo). */
const TEXT_FIELDS = [
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

export const CORRECTABLE_FIELDS = Object.freeze([...TEXT_FIELDS, ...AMOUNT_FIELDS]);

export class CorrectionError extends Error {}

/**
 * Convierte una corrección escrita por una persona ("total=1,180.50",
 * "issue_date=15/10/2026", "supplier_ruc=") en el valor que se guarda,
 * aplicando la misma normalización que a los datos extraídos. Un valor vacío
 * borra el dato.
 *
 * @returns {{ field: string, column: string, value: string | number | null, display: string }}
 */
export function parseCorrection(field, raw) {
  if (!CORRECTABLE_FIELDS.includes(field)) {
    throw new CorrectionError(
      `No se puede corregir "${field}". Campos permitidos: ${CORRECTABLE_FIELDS.join(', ')}`,
    );
  }
  const text = raw === null || raw === undefined ? '' : String(raw).trim();
  if (AMOUNT_FIELDS.includes(field)) {
    let cents;
    try {
      cents = text === '' ? null : toCents(text);
    } catch {
      throw new CorrectionError(`Importe no válido para ${field}: "${raw}"`);
    }
    return {
      field,
      column: `${field}_cents`,
      value: cents,
      display: cents === null ? '' : formatCents(cents),
    };
  }
  // Se reutiliza la normalización de la extracción: misma regla para la IA y para las personas.
  const { invoice } = normalizeInvoice({ [field]: text === '' ? null : text });
  const value = invoice[field] ?? null;
  return { field, column: field, value, display: value ?? '' };
}

/** "total=118.00" → ["total", "118.00"]. El valor puede contener "=". */
export function splitAssignment(assignment) {
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
export function isPreservedOnRevalidation(ruleCode) {
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
