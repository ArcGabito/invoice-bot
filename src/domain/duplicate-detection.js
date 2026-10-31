import { FOREIGN_DOCUMENT_TYPE } from './invoice.js';

/** "Cloudflare, Inc." y "CLOUDFLARE INC" deben dar el mismo emisor. */
function normalizeName(name) {
  return String(name)
    .normalize('NFD')
    .replace(/\p{Diacritic}/gu, '')
    .toUpperCase()
    .replace(/[^A-Z0-9]+/g, ' ')
    .trim();
}

/**
 * Clave de negocio de un comprobante: RUC emisor + tipo + serie + número.
 * El número se compara sin ceros a la izquierda ("00001234" = "1234").
 * Un comprobante del exterior no tiene RUC ni serie: se identifica por el
 * identificador fiscal del proveedor (o su nombre) y el número de factura.
 * Devuelve null si falta alguna parte: sin clave completa no se puede afirmar
 * que dos comprobantes sean el mismo.
 */
export function businessKey({
  supplier_ruc,
  supplier_tax_id,
  supplier_name,
  document_type,
  series,
  number,
}) {
  if (document_type === FOREIGN_DOCUMENT_TYPE) {
    const supplier = supplier_tax_id
      ? `ID:${String(supplier_tax_id)
          .toUpperCase()
          .replace(/[^A-Z0-9]/g, '')}`
      : supplier_name && `NOMBRE:${normalizeName(supplier_name)}`;
    if (!supplier || !number) return null;
    return [supplier, document_type, String(number).toUpperCase()].join('|');
  }
  if (!supplier_ruc || !document_type || !series || !number) return null;
  const normalizedNumber = String(number).replace(/^0+(?=\d)/, '');
  return [supplier_ruc, document_type, String(series).toUpperCase(), normalizedNumber].join('|');
}

export const DUPLICATE_KIND = Object.freeze({
  /** Mismo contenido binario (SHA-256) en otra ruta. */
  EXACT: 'exact',
  /** Archivos distintos con la misma clave de negocio. */
  POSSIBLE: 'possible',
});
