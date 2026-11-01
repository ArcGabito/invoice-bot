import { SEVERITY } from './invoice.ts';
import type { InvoiceData, NewIssue } from './invoice.ts';

/**
 * Tipo de cambio para registrar en soles los comprobantes en otra moneda: el
 * de venta publicado por SUNAT para la fecha de emisión o, si ese día no hubo
 * publicación (fines de semana y feriados), el último publicado antes.
 */

/** Días hacia atrás que se buscan si la fecha de emisión no tiene publicación. */
export const EXCHANGE_RATE_LOOKBACK_DAYS = 10;

/** Moneda local: no necesita tipo de cambio. */
export const LOCAL_CURRENCY = 'PEN';

export const needsExchangeRate = (invoice: Pick<InvoiceData, 'currency'>): boolean =>
  invoice.currency !== null && invoice.currency !== LOCAL_CURRENCY;

/** Importe en céntimos de la moneda original → céntimos de sol. */
export function toPenCents(cents: number, rate: number): number {
  // El tipo de cambio de SUNAT tiene 3 decimales: se opera en milésimas para no acumular error.
  return Math.round((cents * Math.round(rate * 1000)) / 1000);
}

export function checkExchangeRate(
  invoice: Pick<InvoiceData, 'currency' | 'issue_date'>,
  rate: { rate: number; date: string } | null,
): NewIssue[] {
  if (!needsExchangeRate(invoice) || rate) return [];
  return [
    {
      rule_code: 'FX_RATE_MISSING',
      severity: SEVERITY.WARNING,
      message: `No hay tipo de cambio de ${invoice.currency} para el ${invoice.issue_date ?? '(sin fecha)'}: los importes en soles no se pueden calcular. Cárgalo con: invoicebot fx set`,
      expected_value: null,
      actual_value: invoice.currency,
    },
  ];
}

/** Reglas que dependen del tipo de cambio: se recalculan al cargar tipos de cambio nuevos. */
export const isExchangeDependentRule = (ruleCode: string): boolean =>
  ruleCode.startsWith('FX_') || ruleCode.startsWith('DETRACTION_');
