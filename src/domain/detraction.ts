import { formatCents } from '../shared/money.ts';
import { SEVERITY } from './invoice.ts';
import type { InvoiceData, NewIssue } from './invoice.ts';
import { toPenCents } from './exchange-rate.ts';

/**
 * Sistema de detracciones del IGV (SPOT, R.S. 183-2004/SUNAT y modificatorias).
 *
 * Códigos del catálogo 54 de SUNAT (Anexo IV) y porcentajes vigentes según
 * la cartilla y los apéndices publicados por SUNAT (orientacion.sunat.gob.pe,
 * consultados el 2026-10-31). Solo se incluyen los códigos confirmados en esas
 * fuentes: un código que no está aquí se registra, pero su porcentaje no se
 * valida. Si SUNAT cambia una tasa, actualiza esta tabla y
 * DETRACTION_RATES_VERSION.
 */
export const DETRACTION_RATES_VERSION = 'spot/2026-10-31';

export interface DetractionCode {
  description: string;
  /** Porcentaje vigente, en centésimas de punto (12 % = 1200). */
  rateBp: number;
  /** Anexo de la R.S. 183-2004/SUNAT. */
  annex: 2 | 3;
}

export const DETRACTION_CODES: Readonly<Record<string, DetractionCode>> = Object.freeze({
  // Anexo 2: bienes
  '004': { description: 'Recursos hidrobiológicos', rateBp: 400, annex: 2 },
  '005': { description: 'Maíz amarillo duro', rateBp: 400, annex: 2 },
  '007': { description: 'Caña de azúcar', rateBp: 1000, annex: 2 },
  '008': { description: 'Madera', rateBp: 400, annex: 2 },
  '009': { description: 'Arena y piedra', rateBp: 1000, annex: 2 },
  '010': { description: 'Residuos, subproductos, desechos y similares', rateBp: 1500, annex: 2 },
  '011': {
    description: 'Bienes gravados con el IGV por renuncia a la exoneración',
    rateBp: 1000,
    annex: 2,
  },
  '014': { description: 'Carnes y despojos comestibles', rateBp: 400, annex: 2 },
  '016': { description: 'Aceite de pescado', rateBp: 1000, annex: 2 },
  '017': {
    description: 'Harina, polvo y pellets de pescado y otros invertebrados acuáticos',
    rateBp: 400,
    annex: 2,
  },
  '023': { description: 'Leche', rateBp: 400, annex: 2 },
  '031': { description: 'Oro gravado con el IGV', rateBp: 1000, annex: 2 },
  '032': { description: 'Páprika y otros frutos de capsicum o pimienta', rateBp: 1000, annex: 2 },
  '034': { description: 'Minerales metálicos no auríferos', rateBp: 1000, annex: 2 },
  '035': { description: 'Bienes exonerados del IGV', rateBp: 150, annex: 2 },
  '036': {
    description: 'Oro y demás minerales metálicos exonerados del IGV',
    rateBp: 150,
    annex: 2,
  },
  '039': { description: 'Minerales no metálicos', rateBp: 1000, annex: 2 },
  '041': { description: 'Plomo', rateBp: 1500, annex: 2 },
  // Anexo 3: servicios
  '012': { description: 'Intermediación laboral y tercerización', rateBp: 1200, annex: 3 },
  '019': { description: 'Arrendamiento de bienes', rateBp: 1000, annex: 3 },
  '020': { description: 'Mantenimiento y reparación de bienes muebles', rateBp: 1200, annex: 3 },
  '021': { description: 'Movimiento de carga', rateBp: 1000, annex: 3 },
  '022': { description: 'Otros servicios empresariales', rateBp: 1200, annex: 3 },
  '024': { description: 'Comisión mercantil', rateBp: 1000, annex: 3 },
  '025': { description: 'Fabricación de bienes por encargo', rateBp: 1000, annex: 3 },
  '026': { description: 'Servicio de transporte de personas', rateBp: 1000, annex: 3 },
  '030': { description: 'Contratos de construcción', rateBp: 400, annex: 3 },
  '037': { description: 'Demás servicios gravados con el IGV', rateBp: 1200, annex: 3 },
});

/** Importe de la operación hasta el cual los servicios del Anexo 3 no están sujetos (S/ 700). */
export const ANNEX3_THRESHOLD_CENTS = 70_000;

/**
 * El depósito se hace en soles y en la práctica se redondea a soles enteros:
 * se acepta una diferencia de hasta S/ 1 con el cálculo exacto.
 */
const AMOUNT_TOLERANCE_CENTS = 100;

const hasDetraction = (i: InvoiceData) =>
  i.detraction_code !== null ||
  i.detraction_percent !== null ||
  i.detraction_amount_cents !== null ||
  i.detraction_account !== null;

const issue = (
  rule_code: string,
  severity: NewIssue['severity'],
  message: string,
  expected: string | null = null,
  actual: string | null = null,
): NewIssue => ({ rule_code, severity, message, expected_value: expected, actual_value: actual });

const formatPercent = (bp: number) => `${(bp / 100).toFixed(2).replace(/\.00$/, '')} %`;

/**
 * Comprueba la detracción que declara el comprobante. No deduce si una
 * operación debería estar sujeta (depende de qué se compró): solo valida lo
 * declarado. `exchangeRate` convierte el total a soles en comprobantes en
 * otra moneda; sin él no se comprueba el monto.
 */
export function checkDetraction(
  invoice: InvoiceData,
  { exchangeRate = null }: { exchangeRate?: number | null } = {},
): NewIssue[] {
  if (!hasDetraction(invoice)) return [];
  const issues: NewIssue[] = [];
  const code = invoice.detraction_code;
  const percent = invoice.detraction_percent;
  const amount = invoice.detraction_amount_cents;

  const missing = [
    code === null && 'código del bien o servicio',
    percent === null && 'porcentaje',
    amount === null && 'monto',
  ].filter(Boolean);
  if (missing.length) {
    issues.push(
      issue(
        'DETRACTION_INCOMPLETE',
        SEVERITY.WARNING,
        `El comprobante indica detracción pero falta: ${missing.join(', ')}`,
      ),
    );
  }

  const known = code ? DETRACTION_CODES[code] : undefined;
  if (code && !known) {
    issues.push(
      issue(
        'DETRACTION_UNKNOWN_CODE',
        SEVERITY.INFO,
        `Código de detracción ${code} sin tasa registrada en InvoiceBot: confirma el porcentaje con tu contador`,
        null,
        code,
      ),
    );
  }
  const percentBp = percent === null ? null : Math.round(percent * 100);
  if (known && percentBp !== null && percentBp !== known.rateBp) {
    issues.push(
      issue(
        'DETRACTION_RATE_MISMATCH',
        SEVERITY.WARNING,
        `El porcentaje de detracción no coincide con la tasa de SUNAT para ${code} (${known.description})`,
        formatPercent(known.rateBp),
        formatPercent(percentBp),
      ),
    );
  }

  const totalPen = totalInPen(invoice, exchangeRate);
  if (amount !== null && percentBp !== null && totalPen !== null) {
    const expected = Math.round((totalPen * percentBp) / 10_000);
    if (Math.abs(expected - amount) > AMOUNT_TOLERANCE_CENTS) {
      issues.push(
        issue(
          'DETRACTION_AMOUNT_MISMATCH',
          SEVERITY.WARNING,
          `El monto de detracción no corresponde al ${formatPercent(percentBp)} del total en soles`,
          formatCents(expected),
          formatCents(amount),
        ),
      );
    }
  }
  if (known?.annex === 3 && totalPen !== null && totalPen <= ANNEX3_THRESHOLD_CENTS) {
    issues.push(
      issue(
        'DETRACTION_BELOW_THRESHOLD',
        SEVERITY.INFO,
        `El total no supera S/ 700: los servicios del Anexo 3 no están sujetos a detracción por ese importe`,
        '> 700.00',
        formatCents(totalPen),
      ),
    );
  }
  return issues;
}

/** Total del comprobante en soles; null si falta el total o el tipo de cambio. */
export function totalInPen(invoice: InvoiceData, exchangeRate: number | null): number | null {
  if (invoice.total_cents === null) return null;
  if (!invoice.currency || invoice.currency === 'PEN') return invoice.total_cents;
  return exchangeRate === null ? null : toPenCents(invoice.total_cents, exchangeRate);
}

/**
 * Neto a pagar al proveedor, en la moneda del comprobante: el total menos la
 * detracción (que se deposita en soles en el Banco de la Nación). null si no
 * se puede calcular.
 */
export function netPayableCents(invoice: InvoiceData, exchangeRate: number | null): number | null {
  if (invoice.total_cents === null) return null;
  const detraction = invoice.detraction_amount_cents;
  if (detraction === null) return invoice.total_cents;
  if (!invoice.currency || invoice.currency === 'PEN') return invoice.total_cents - detraction;
  if (exchangeRate === null || exchangeRate <= 0) return null;
  return invoice.total_cents - Math.round(detraction / exchangeRate);
}
