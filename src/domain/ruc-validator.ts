/**
 * Validación matemática del RUC peruano (11 dígitos, módulo 11).
 *
 * Que un RUC pase esta validación NO confirma que el contribuyente exista,
 * esté activo o que el comprobante sea auténtico: eso requiere consultar
 * fuentes externas autorizadas (SUNAT) y es una funcionalidad independiente.
 */

const WEIGHTS = [5, 4, 3, 2, 7, 6, 5, 4, 3, 2];

/** Prefijos válidos: 10 persona natural, 15/16/17 casos especiales, 20 persona jurídica. */
const VALID_PREFIXES = new Set(['10', '15', '16', '17', '20']);

export function computeRucCheckDigit(firstTenDigits: string): number {
  const sum = WEIGHTS.reduce((acc, weight, i) => acc + weight * Number(firstTenDigits[i]), 0);
  const digit = 11 - (sum % 11);
  if (digit === 10) return 0;
  if (digit === 11) return 1;
  return digit;
}

export type RucValidation =
  | { valid: true }
  | { valid: false; reason: 'format' | 'prefix' | 'check_digit'; expected?: string };

export function validateRuc(ruc: unknown): RucValidation {
  if (typeof ruc !== 'string' || !/^\d{11}$/.test(ruc)) return { valid: false, reason: 'format' };
  if (!VALID_PREFIXES.has(ruc.slice(0, 2))) return { valid: false, reason: 'prefix' };
  const expected = computeRucCheckDigit(ruc.slice(0, 10));
  if (expected !== Number(ruc[10])) {
    return { valid: false, reason: 'check_digit', expected: String(expected) };
  }
  return { valid: true };
}

/** Elimina espacios, guiones y puntos que a veces acompañan al RUC impreso. */
export function normalizeRuc(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  const cleaned = String(value).replace(/[\s.-]/g, '');
  return cleaned === '' ? null : cleaned;
}
