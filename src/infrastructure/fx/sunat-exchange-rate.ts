import { InvoiceBotError, errorMessage } from '../../shared/errors.ts';
import type { Config } from '../../shared/config.ts';
import type { ExchangeRateInput } from '../../application/use-cases/exchange-rates.ts';

/**
 * Tipo de cambio del dólar publicado por SUNAT (el de la SBS que SUNAT usa
 * para fines tributarios). SUNAT ofrece un archivo de texto público con el
 * del día, sin credenciales: "31/10/2026|3.458|3.467|" (fecha|compra|venta).
 *
 * Solo publica el del día: el histórico se va formando al guardarlo cada día
 * (`invoicebot run` lo descarga en cada ciclo). Las fechas anteriores se
 * cargan con `invoicebot fx set` o `fx import`. No se envía ningún dato.
 */
export const SUNAT_EXCHANGE_RATE_URL = 'https://www.sunat.gob.pe/a/txt/tipoCambio.txt';

export class ExchangeRateError extends InvoiceBotError {
  constructor(message: string, { cause }: { cause?: unknown } = {}) {
    super(message, { code: 'EXCHANGE_RATE_ERROR', cause });
  }
}

/** "31/10/2026|3.458|3.467|" → tipo de cambio del dólar de ese día. */
export function parseSunatExchangeRate(body: string): ExchangeRateInput {
  const m = /^\s*(\d{2})\/(\d{2})\/(\d{4})\|(\d+(?:\.\d+)?)\|(\d+(?:\.\d+)?)\|?\s*$/.exec(body);
  if (!m) {
    throw new ExchangeRateError(
      `Respuesta de SUNAT no reconocida: ${JSON.stringify(body.slice(0, 80))}`,
    );
  }
  const sell = Number(m[5]);
  if (!(sell > 0)) throw new ExchangeRateError(`Tipo de cambio no válido: ${m[5]}`);
  return {
    currency: 'USD',
    date: `${m[3]}-${m[2]}-${m[1]}`,
    buy: Number(m[4]),
    sell,
    source: 'sunat',
  };
}

export interface ExchangeRateSource {
  /** Tipo de cambio del dólar publicado hoy. */
  fetchToday(): Promise<ExchangeRateInput>;
}

export function createSunatExchangeRateSource({
  url = SUNAT_EXCHANGE_RATE_URL,
  timeoutMs = 20_000,
  fetchImpl = globalThis.fetch,
}: {
  url?: string;
  timeoutMs?: number;
  fetchImpl?: typeof globalThis.fetch;
} = {}): ExchangeRateSource {
  return {
    async fetchToday() {
      let res: Response;
      try {
        res = await fetchImpl(url, {
          headers: { 'user-agent': 'InvoiceBot', accept: 'text/plain' },
          signal: AbortSignal.timeout(timeoutMs),
        });
      } catch (error) {
        throw new ExchangeRateError(
          `No se pudo descargar el tipo de cambio de SUNAT: ${errorMessage(error)}`,
          { cause: error },
        );
      }
      if (!res.ok) {
        throw new ExchangeRateError(`SUNAT respondió ${res.status} al pedir el tipo de cambio`);
      }
      return parseSunatExchangeRate(await res.text());
    },
  };
}

export const exchangeRateSourceFromConfig = (config: Pick<Config, 'fx'>): ExchangeRateSource =>
  createSunatExchangeRateSource({
    url: config.fx.sunatUrl ?? SUNAT_EXCHANGE_RATE_URL,
    timeoutMs: config.fx.timeoutMs,
  });

/**
 * Tipos de cambio en CSV para cargar fechas anteriores: una fila por día con
 * fecha, compra y venta (p. ej. copiados de la consulta de SUNAT a una hoja
 * de cálculo). Separador coma, punto y coma, barra vertical o tabulador;
 * fecha DD/MM/AAAA o AAAA-MM-DD; la fila de encabezado es opcional. Una sola
 * columna de importe se toma como venta.
 */
export function parseExchangeRateCsv(
  text: string,
  { currency = 'USD' }: { currency?: string } = {},
): { rates: ExchangeRateInput[]; errors: string[] } {
  const rates: ExchangeRateInput[] = [];
  const errors: string[] = [];
  text.split(/\r?\n/).forEach((line, index) => {
    const cells = line
      .split(/[,;|\t]/)
      .map((c) => c.trim())
      .filter((c) => c !== '');
    if (cells.length === 0) return;
    const date = parseDate(cells[0]!);
    if (!date) {
      // Encabezado: primera línea sin fecha.
      if (index > 0 || /\d/.test(cells[0]!)) errors.push(`Línea ${index + 1}: fecha no válida`);
      return;
    }
    const numbers = cells.slice(1).map(Number);
    if (numbers.length === 0 || numbers.some((n) => !(n > 0))) {
      errors.push(`Línea ${index + 1}: importes no válidos`);
      return;
    }
    const [buy, sell] = numbers.length === 1 ? [null, numbers[0]!] : [numbers[0]!, numbers[1]!];
    rates.push({ currency, date, buy, sell, source: 'csv' });
  });
  return { rates, errors };
}

/** DD/MM/AAAA o AAAA-MM-DD → AAAA-MM-DD; null si no es una fecha que exista. */
export function parseDate(value: string): string | null {
  let iso: string | null = null;
  let m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (m) iso = value;
  m = /^(\d{1,2})\/(\d{1,2})\/(\d{4})$/.exec(value);
  if (m) iso = `${m[3]}-${m[2]!.padStart(2, '0')}-${m[1]!.padStart(2, '0')}`;
  if (!iso) return null;
  const date = new Date(`${iso}T00:00:00Z`);
  return !Number.isNaN(date.getTime()) && date.toISOString().slice(0, 10) === iso ? iso : null;
}
