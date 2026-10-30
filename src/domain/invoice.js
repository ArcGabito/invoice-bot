import { toCents } from '../shared/money.js';
import { normalizeRuc } from './ruc-validator.js';

/** Estados persistentes de un documento a lo largo del pipeline. */
export const DOCUMENT_STATUS = Object.freeze({
  PENDING: 'pending',
  PROCESSING: 'processing',
  EXTRACTED: 'extracted',
  FAILED: 'failed',
  /** El documento no tiene texto suficiente; se procesará cuando haya OCR (v0.2.0). */
  NEEDS_OCR: 'needs_ocr',
  /** No se procesa por diseño: representación impresa de un XML ya leído, CDR, etc. */
  SKIPPED: 'skipped',
});

/** Motivos de DOCUMENT_STATUS.SKIPPED (documents.skip_reason). */
export const SKIP_REASON = Object.freeze({
  /** PDF con el mismo nombre que un XML ya extraído: sus datos vienen del XML. */
  PRINTED_REPRESENTATION: 'printed_representation',
  /** Constancia de recepción de SUNAT (ApplicationResponse). */
  CDR: 'cdr',
  /** XML que no es un comprobante UBL. */
  UNSUPPORTED_XML: 'unsupported_xml',
});

/** De dónde salen los datos de una factura. */
export const DATA_SOURCE = Object.freeze({ AI: 'ai', XML: 'xml' });

export const VALIDATION_STATUS = Object.freeze({
  VALID: 'valid',
  WARNING: 'warning',
  ERROR: 'error',
});

export const SEVERITY = Object.freeze({ ERROR: 'error', WARNING: 'warning', INFO: 'info' });

/** Catálogo 01 de SUNAT (tipos de comprobante más habituales). */
export const DOCUMENT_TYPES = Object.freeze({
  '01': 'Factura',
  '03': 'Boleta de venta',
  '07': 'Nota de crédito',
  '08': 'Nota de débito',
  12: 'Ticket de máquina registradora',
  14: 'Recibo de servicios públicos',
});

/** Campos monetarios del comprobante, en el orden en que se exportan. */
export const AMOUNT_FIELDS = Object.freeze([
  'subtotal',
  'taxable_amount',
  'exempt_amount',
  'unaffected_amount',
  'discount_amount',
  'igv',
  'isc',
  'icbper',
  'other_charges',
  'other_taxes',
  'total',
]);

function cleanText(value) {
  if (value === null || value === undefined) return null;
  const text = String(value).trim().replace(/\s+/g, ' ');
  return text === '' ? null : text;
}

/** Acepta AAAA-MM-DD y DD/MM/AAAA (o DD-MM-AAAA). No valida el calendario. */
export function normalizeDate(value) {
  const text = cleanText(value);
  if (text === null) return null;
  let m = /^(\d{4})-(\d{1,2})-(\d{1,2})$/.exec(text);
  if (m) return `${m[1]}-${m[2].padStart(2, '0')}-${m[3].padStart(2, '0')}`;
  m = /^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$/.exec(text);
  if (m) return `${m[3]}-${m[2].padStart(2, '0')}-${m[1].padStart(2, '0')}`;
  return text;
}

function normalizeDocumentType(value) {
  const text = cleanText(value);
  if (text === null) return null;
  if (/^\d{1,2}$/.test(text)) return text.padStart(2, '0');
  const lower = text.toLowerCase();
  if (lower.includes('nota de cr')) return '07';
  if (lower.includes('nota de d')) return '08';
  if (lower.includes('boleta')) return '03';
  if (lower.includes('factura')) return '01';
  return text;
}

function normalizeCurrency(value) {
  const text = cleanText(value);
  if (text === null) return null;
  const upper = text.toUpperCase();
  if (upper === 'S/' || upper === 'S/.' || upper === 'SOLES') return 'PEN';
  if (upper === 'US$' || upper === '$' || upper === 'DOLARES' || upper === 'DÓLARES') return 'USD';
  return upper;
}

/**
 * Convierte la salida cruda del modelo en una factura normalizada.
 * Nunca inventa valores: lo que no se puede interpretar queda como null
 * y se informa en `normalizationIssues` para que la validación lo marque.
 *
 * @param {Record<string, unknown>} extraction
 */
export function normalizeInvoice(extraction) {
  const normalizationIssues = [];
  const amounts = {};
  for (const field of AMOUNT_FIELDS) {
    try {
      amounts[`${field}_cents`] = toCents(extraction[field] ?? null);
    } catch {
      amounts[`${field}_cents`] = null;
      normalizationIssues.push({ field, value: String(extraction[field]) });
    }
  }

  const series = cleanText(extraction.series)?.toUpperCase() ?? null;
  const number = cleanText(extraction.number)?.replace(/\s/g, '') ?? null;

  return {
    invoice: {
      supplier_name: cleanText(extraction.supplier_name),
      supplier_ruc: normalizeRuc(extraction.supplier_ruc),
      customer_name: cleanText(extraction.customer_name),
      customer_ruc: normalizeRuc(extraction.customer_ruc),
      document_type: normalizeDocumentType(extraction.document_type),
      series,
      number,
      issue_date: normalizeDate(extraction.issue_date),
      due_date: normalizeDate(extraction.due_date),
      currency: normalizeCurrency(extraction.currency),
      ...amounts,
    },
    normalizationIssues,
  };
}
