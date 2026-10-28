/**
 * Clave de negocio de un comprobante: RUC emisor + tipo + serie + número.
 * El número se compara sin ceros a la izquierda ("00001234" = "1234").
 * Devuelve null si falta alguna parte: sin clave completa no se puede afirmar
 * que dos comprobantes sean el mismo.
 */
export function businessKey({ supplier_ruc, document_type, series, number }) {
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
