/**
 * Importes monetarios como enteros en céntimos. Nunca se opera con coma
 * flotante: los valores se convierten desde su representación decimal en texto.
 */

const DECIMAL_PATTERN = /^(-)?(\d+)(?:\.(\d+))?$/;

/**
 * Normaliza un importe escrito por una persona o un modelo a una cadena
 * decimal con punto ("1234.56"). Acepta "S/ 1,234.56", "1.234,56", "US$ 10".
 * Devuelve null si el valor está vacío y lanza si no es interpretable.
 */
export function normalizeDecimalString(value) {
  if (value === null || value === undefined) return null;
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) throw new TypeError(`Importe no finito: ${value}`);
    // toString de un número JS es la representación decimal más corta que lo
    // identifica, así que 0.1 + 0.2 no llega aquí como 0.30000000000000004
    // salvo que el propio origen lo haya calculado así.
    value = String(value);
  }
  if (typeof value !== 'string') throw new TypeError(`Importe no interpretable: ${value}`);

  let text = value.trim().replace(/\s+/g, '');
  if (text === '' || text === '-') return null;

  let negative = false;
  if (/^\(.*\)$/.test(text)) {
    negative = true;
    text = text.slice(1, -1);
  }
  // Símbolos y códigos de moneda habituales en comprobantes peruanos.
  text = text.replace(/^(S\/\.?|US\$|USD|PEN|EUR|\$|€)/i, '').replace(/(S\/\.?|USD|PEN|EUR)$/i, '');
  if (text.startsWith('-')) {
    negative = !negative;
    text = text.slice(1);
  }

  const lastDot = text.lastIndexOf('.');
  const lastComma = text.lastIndexOf(',');
  if (lastDot !== -1 && lastComma !== -1) {
    // El separador que aparece al final es el decimal.
    const decimalSep = lastDot > lastComma ? '.' : ',';
    const thousandSep = decimalSep === '.' ? ',' : '.';
    text = text.split(thousandSep).join('').replace(decimalSep, '.');
  } else if (lastComma !== -1) {
    const parts = text.split(',');
    // "1,234" o "1,234,567" → miles; "12,5" o "12,50" → decimal.
    const looksLikeThousands = parts.length > 2 || (parts.length === 2 && parts[1].length === 3);
    text = looksLikeThousands ? parts.join('') : parts.join('.');
  } else if (lastDot !== -1 && text.split('.').length > 2) {
    // "1.234.567" → separador de miles europeo.
    text = text.split('.').join('');
  }

  const match = DECIMAL_PATTERN.exec(text);
  if (!match) throw new TypeError(`Importe no interpretable: "${value}"`);
  const [, sign, int, frac = ''] = match;
  return `${negative !== Boolean(sign) ? '-' : ''}${int}${frac ? `.${frac}` : ''}`;
}

/**
 * Convierte un importe a céntimos con redondeo comercial (mitad hacia arriba,
 * alejándose de cero). Devuelve null para valores vacíos.
 */
export function toCents(value) {
  const normalized = normalizeDecimalString(value);
  if (normalized === null) return null;
  const [, sign, int, frac = ''] = DECIMAL_PATTERN.exec(normalized);
  const padded = frac.padEnd(3, '0');
  let cents = Number(int) * 100 + Number(padded.slice(0, 2));
  if (Number(padded[2]) >= 5) cents += 1;
  if (!Number.isSafeInteger(cents)) throw new RangeError(`Importe fuera de rango: "${value}"`);
  return sign && cents !== 0 ? -cents : cents;
}

/** Formatea céntimos como cadena decimal estable: 11800 → "118.00". */
export function formatCents(cents) {
  if (cents === null || cents === undefined) return '';
  const sign = cents < 0 ? '-' : '';
  const abs = Math.abs(cents);
  return `${sign}${Math.trunc(abs / 100)}.${String(abs % 100).padStart(2, '0')}`;
}

/** Suma céntimos ignorando valores nulos. */
export function sumCents(...values) {
  return values.reduce((acc, v) => (v === null || v === undefined ? acc : acc + v), 0);
}

/**
 * Multiplica céntimos por una tasa expresada en puntos básicos (1800 = 18 %),
 * con redondeo mitad hacia arriba. Solo opera con enteros.
 */
export function applyRateBasisPoints(cents, basisPoints) {
  const product = cents * basisPoints;
  const quotient = Math.trunc(product / 10000);
  const remainder = Math.abs(product % 10000);
  if (remainder * 2 >= 10000) return quotient + Math.sign(product);
  return quotient;
}
