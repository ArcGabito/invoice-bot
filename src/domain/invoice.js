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
  91: 'Comprobante de no domiciliado (proveedor del exterior)',
});

/**
 * Comprobante de un proveedor del exterior (tabla 10 de SUNAT, código 91):
 * no tiene RUC, serie SUNAT ni IGV.
 */
export const FOREIGN_DOCUMENT_TYPE = '91';

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
  /** Anticipos aplicados: el total ya los descuenta. */
  'prepaid_amount',
]);

function cleanText(value) {
  if (value === null || value === undefined) return null;
  const text = String(value).trim().replace(/\s+/g, ' ');
  return text === '' ? null : text;
}

const MONTHS = {
  jan: 1,
  january: 1,
  ene: 1,
  enero: 1,
  feb: 2,
  february: 2,
  febrero: 2,
  mar: 3,
  march: 3,
  marzo: 3,
  apr: 4,
  april: 4,
  abr: 4,
  abril: 4,
  may: 5,
  mayo: 5,
  jun: 6,
  june: 6,
  junio: 6,
  jul: 7,
  july: 7,
  julio: 7,
  aug: 8,
  august: 8,
  ago: 8,
  agosto: 8,
  sep: 9,
  sept: 9,
  september: 9,
  set: 9,
  septiembre: 9,
  setiembre: 9,
  oct: 10,
  october: 10,
  octubre: 10,
  nov: 11,
  november: 11,
  noviembre: 11,
  dec: 12,
  december: 12,
  dic: 12,
  diciembre: 12,
};

const iso = (y, m, d) => `${y}-${String(m).padStart(2, '0')}-${String(d).padStart(2, '0')}`;

/**
 * Acepta AAAA-MM-DD, DD/MM/AAAA (o DD-MM-AAAA) y el mes en letras, en inglés
 * ("August 5, 2025") o en español ("5 de agosto de 2025"). No valida el calendario.
 */
export function normalizeDate(value) {
  const text = cleanText(value);
  if (text === null) return null;
  let m = /^(\d{4})-(\d{1,2})-(\d{1,2})$/.exec(text);
  if (m) return iso(m[1], m[2], m[3]);
  m = /^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$/.exec(text);
  if (m) return iso(m[3], m[2], m[1]);
  m = /^([a-záé]+)\.? (\d{1,2}),? (\d{4})$/i.exec(text);
  if (m && MONTHS[m[1].toLowerCase()]) return iso(m[3], MONTHS[m[1].toLowerCase()], m[2]);
  m = /^(\d{1,2})(?: de)? ([a-záé]+)\.?(?: de)? (\d{4})$/i.exec(text);
  if (m && MONTHS[m[2].toLowerCase()]) return iso(m[3], MONTHS[m[2].toLowerCase()], m[1]);
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

/** Nombres de país habituales en facturas de proveedores → código ISO 3166. */
const COUNTRY_CODES = {
  'UNITED STATES': 'US',
  'UNITED STATES OF AMERICA': 'US',
  USA: 'US',
  'U.S.A.': 'US',
  'U.S.': 'US',
  'ESTADOS UNIDOS': 'US',
  'EE. UU.': 'US',
  'EE.UU.': 'US',
  IRELAND: 'IE',
  IRLANDA: 'IE',
  'UNITED KINGDOM': 'GB',
  UK: 'GB',
  'REINO UNIDO': 'GB',
  CANADA: 'CA',
  CANADÁ: 'CA',
  GERMANY: 'DE',
  ALEMANIA: 'DE',
  NETHERLANDS: 'NL',
  'PAÍSES BAJOS': 'NL',
  SINGAPORE: 'SG',
  SINGAPUR: 'SG',
  MEXICO: 'MX',
  MÉXICO: 'MX',
  SPAIN: 'ES',
  ESPAÑA: 'ES',
  CHINA: 'CN',
  PERU: 'PE',
  PERÚ: 'PE',
};

/** Código ISO 3166 de dos letras; un nombre de país conocido se convierte y el resto se conserva tal cual. */
function normalizeCountry(value) {
  const text = cleanText(value);
  if (text === null) return null;
  if (/^[a-z]{2}$/i.test(text)) return text.toUpperCase();
  return COUNTRY_CODES[text.toUpperCase()] ?? text;
}

/**
 * ¿La extracción tiene los datos mínimos de un comprobante? Tipo conocido,
 * número, emisor identificado (RUC, identificador fiscal o nombre) y total.
 * Sirve para no descartar un comprobante real solo porque el modelo dudó.
 */
export function hasInvoiceData(extraction) {
  const type = normalizeDocumentType(extraction.document_type);
  const supplier =
    extraction.supplier_ruc || extraction.supplier_tax_id || extraction.supplier_name;
  return Boolean(
    type &&
    DOCUMENT_TYPES[type] &&
    extraction.number &&
    supplier &&
    extraction.total !== null &&
    extraction.total !== undefined,
  );
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
      supplier_tax_id: cleanText(extraction.supplier_tax_id),
      supplier_country: normalizeCountry(extraction.supplier_country),
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
