import { XMLParser } from 'fast-xml-parser';
import { DocumentReadError } from '../../shared/errors.js';
import { formatCents, toCents } from '../../shared/money.js';
import { ExtractionSchema } from '../ai/extraction-schema.js';

/**
 * Versión del lector UBL. Se guarda en extraction_runs.prompt_version para
 * poder auditar con qué lógica se leyó cada XML.
 */
export const UBL_PARSER_VERSION = 'ubl-2.1-sunat/2026-10-30.1';

/** Tipo de comprobante según el elemento raíz del XML. */
const ROOT_DOCUMENT_TYPES = { Invoice: null, CreditNote: '07', DebitNote: '08' };

/**
 * Códigos del catálogo 05 de SUNAT (tipos de tributo) que se usan para
 * repartir los TaxSubtotal en los campos del comprobante.
 */
const TAX_SCHEMES = {
  IGV: '1000',
  IVAP: '1016',
  ISC: '2000',
  ICBPER: '7152',
  EXPORT: '9995',
  FREE: '9996',
  EXEMPT: '9997',
  UNAFFECTED: '9998',
  OTHER: '9999',
};

const ARRAY_ELEMENTS = new Set([
  'TaxTotal',
  'TaxSubtotal',
  'PartyIdentification',
  'PartyLegalEntity',
  'PartyName',
  'Note',
]);

const parser = new XMLParser({
  ignoreAttributes: false,
  attributeNamePrefix: '@_',
  removeNSPrefix: true,
  // Todo como texto: "00001234" no debe convertirse en 1234 ni "118.10" en 118.1.
  parseTagValue: false,
  parseAttributeValue: false,
  trimValues: true,
  processEntities: true,
  isArray: (name) => ARRAY_ELEMENTS.has(name),
});

/** Decodifica respetando la codificación declarada (SUNAT admite UTF-8 e ISO-8859-1). */
export function decodeXml(buffer) {
  const prolog = buffer.subarray(0, 200).toString('latin1');
  const declared = /encoding\s*=\s*["']([^"']+)["']/i.exec(prolog)?.[1]?.toLowerCase();
  const encoding =
    declared === 'iso-8859-1' || declared === 'latin1' || declared === 'windows-1252'
      ? 'latin1'
      : 'utf-8';
  return new TextDecoder(encoding).decode(buffer);
}

const text = (node) => {
  if (node === undefined || node === null) return null;
  if (Array.isArray(node)) return text(node[0]);
  if (typeof node === 'object')
    return node['#text'] === undefined ? null : String(node['#text']).trim() || null;
  const value = String(node).trim();
  return value === '' ? null : value;
};

const first = (node) => (Array.isArray(node) ? node[0] : node);

function readParty(partyRoot) {
  const party = first(partyRoot)?.Party;
  if (!party) return { name: null, id: null };
  const legal = first(party.PartyLegalEntity);
  const name = text(legal?.RegistrationName) ?? text(first(party.PartyName)?.Name);
  // Preferir el documento con schemeID 6 (RUC); si no hay, el primero (DNI = 1, etc.).
  const ids = (party.PartyIdentification ?? []).map((p) => p.ID).filter(Boolean);
  const ruc = ids.find((id) => typeof id === 'object' && id['@_schemeID'] === '6');
  return { name, id: text(ruc ?? ids[0]) };
}

/**
 * Suma importes en céntimos. Un valor no numérico se devuelve tal cual para
 * que la validación lo marque como ilegible en lugar de ocultarlo.
 */
function sumAmounts(values) {
  const present = values.filter((v) => v !== null);
  if (present.length <= 1) return present[0] ?? null;
  try {
    return formatCents(present.reduce((acc, v) => acc + toCents(v), 0));
  } catch {
    return present.join(' + ');
  }
}

function readTaxes(root) {
  const byScheme = {};
  for (const total of root.TaxTotal ?? []) {
    for (const sub of total.TaxSubtotal ?? []) {
      const scheme = text(sub.TaxCategory?.TaxScheme?.ID);
      if (!scheme) continue;
      byScheme[scheme] ??= { taxable: [], tax: [] };
      byScheme[scheme].taxable.push(text(sub.TaxableAmount));
      byScheme[scheme].tax.push(text(sub.TaxAmount));
    }
  }
  const taxable = (code) => sumAmounts(byScheme[code]?.taxable ?? []);
  const tax = (code) => sumAmounts(byScheme[code]?.tax ?? []);
  return { taxable, tax, schemes: Object.keys(byScheme) };
}

/**
 * Lee un comprobante electrónico SUNAT en UBL 2.1 (factura, boleta, nota de
 * crédito o de débito) y devuelve los datos con la misma forma que la
 * extracción por IA, para que pasen por la misma normalización y validación.
 *
 * No verifica la firma digital: confirma la estructura y los datos, no la
 * autenticidad del documento.
 *
 * @returns {{ kind: 'invoice', data: object } | { kind: 'cdr' } | { kind: 'other', root: string }}
 */
export function parseUbl(buffer) {
  let doc;
  try {
    doc = parser.parse(decodeXml(buffer));
  } catch (error) {
    throw new DocumentReadError(`XML mal formado: ${error.message}`, { cause: error });
  }

  const rootName = Object.keys(doc).find((k) => !k.startsWith('?') && !k.startsWith('#'));
  if (!rootName) throw new DocumentReadError('El XML no tiene elemento raíz');
  // Constancia de recepción de SUNAT: acompaña al comprobante, pero no lo es.
  if (rootName === 'ApplicationResponse') return { kind: 'cdr' };
  if (!(rootName in ROOT_DOCUMENT_TYPES)) return { kind: 'other', root: rootName };

  const root = doc[rootName];
  const notes = [];
  const [series = null, number = null] = (text(root.ID) ?? '')
    .split('-')
    .map((s) => s.trim() || null);
  const documentType = ROOT_DOCUMENT_TYPES[rootName] ?? text(root.InvoiceTypeCode);
  const supplier = readParty(root.AccountingSupplierParty);
  const customer = readParty(root.AccountingCustomerParty);
  const totals = root.LegalMonetaryTotal ?? root.RequestedMonetaryTotal ?? {};
  const { taxable, tax, schemes } = readTaxes(root);

  // Las exportaciones no llevan IGV; para la aritmética del total se agrupan con las inafectas.
  const unaffected = sumAmounts([taxable(TAX_SCHEMES.UNAFFECTED), taxable(TAX_SCHEMES.EXPORT)]);
  if (schemes.includes(TAX_SCHEMES.EXPORT))
    notes.push('Incluye operaciones de exportación (sumadas a inafectas).');
  if (schemes.includes(TAX_SCHEMES.FREE))
    notes.push('Incluye operaciones gratuitas, que no forman parte del total.');
  if (schemes.includes(TAX_SCHEMES.IVAP))
    notes.push('Incluye IVAP (registrado en otros tributos).');

  const data = {
    is_invoice: true,
    supplier_name: supplier.name,
    supplier_ruc: supplier.id,
    customer_name: customer.name,
    customer_ruc: customer.id,
    document_type: documentType,
    series,
    number,
    issue_date: text(root.IssueDate),
    due_date: text(root.DueDate) ?? text(first(root.PaymentTerms)?.PaymentDueDate),
    currency: text(root.DocumentCurrencyCode),
    subtotal: text(totals.LineExtensionAmount),
    taxable_amount: taxable(TAX_SCHEMES.IGV),
    exempt_amount: taxable(TAX_SCHEMES.EXEMPT),
    unaffected_amount: unaffected,
    discount_amount: text(totals.AllowanceTotalAmount),
    igv: tax(TAX_SCHEMES.IGV),
    isc: tax(TAX_SCHEMES.ISC),
    icbper: tax(TAX_SCHEMES.ICBPER),
    other_charges: text(totals.ChargeTotalAmount),
    other_taxes: sumAmounts([tax(TAX_SCHEMES.OTHER), tax(TAX_SCHEMES.IVAP)]),
    total: text(totals.PayableAmount),
    prepaid_amount: text(totals.PrepaidAmount),
    notes,
  };
  return { kind: 'invoice', data: ExtractionSchema.parse(data) };
}
