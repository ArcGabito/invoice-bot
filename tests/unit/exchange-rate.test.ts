import { describe, expect, it, vi } from 'vitest';
import { normalizeInvoice } from '../../src/domain/invoice.ts';
import { validateInvoice } from '../../src/domain/invoice-validation.ts';
import {
  checkExchangeRate,
  isExchangeDependentRule,
  toPenCents,
} from '../../src/domain/exchange-rate.ts';
import {
  ExchangeRateError,
  createSunatExchangeRateSource,
  parseDate,
  parseExchangeRateCsv,
  parseSunatExchangeRate,
} from '../../src/infrastructure/fx/sunat-exchange-rate.ts';

const usdInvoice = () =>
  normalizeInvoice({
    supplier_name: 'Proveedor de ejemplo S.A.C.',
    supplier_ruc: '20100047218',
    document_type: '01',
    series: 'F001',
    number: '00001234',
    issue_date: '2026-10-15',
    currency: 'USD',
    subtotal: '100.00',
    igv: '18.00',
    total: '118.00',
  });

describe('tipo de cambio', () => {
  it('convierte a soles sin acumular error de coma flotante', () => {
    expect(toPenCents(11_800, 3.75)).toBe(44_250);
    expect(toPenCents(1, 3.467)).toBe(3);
    expect(toPenCents(123_456_789, 3.467)).toBe(428_024_687);
  });

  it('advierte en otra moneda si no hay tipo de cambio, y solo si se buscó', () => {
    const { invoice, normalizationIssues } = usdInvoice();
    expect(checkExchangeRate(invoice, null)).toEqual([
      expect.objectContaining({ rule_code: 'FX_RATE_MISSING', severity: 'warning' }),
    ]);
    expect(checkExchangeRate(invoice, { rate: 3.75, date: '2026-10-15' })).toEqual([]);
    expect(checkExchangeRate({ currency: 'PEN', issue_date: '2026-10-15' }, null)).toEqual([]);

    const options = { normalizationIssues, today: '2026-10-28' };
    expect(validateInvoice(invoice, options).status).toBe('valid');
    expect(validateInvoice(invoice, { ...options, exchangeRate: null }).status).toBe('warning');
    expect(
      validateInvoice(invoice, { ...options, exchangeRate: { rate: 3.75, date: '2026-10-14' } })
        .status,
    ).toBe('valid');
  });

  it('identifica las reglas que se recalculan al cargar tipos de cambio', () => {
    expect(isExchangeDependentRule('FX_RATE_MISSING')).toBe(true);
    expect(isExchangeDependentRule('DETRACTION_AMOUNT_MISMATCH')).toBe(true);
    expect(isExchangeDependentRule('TOTAL_MISMATCH')).toBe(false);
  });
});

describe('tipo de cambio de SUNAT', () => {
  it('lee el archivo del día', () => {
    expect(parseSunatExchangeRate('31/10/2026|3.458|3.467|\n')).toEqual({
      currency: 'USD',
      date: '2026-10-31',
      buy: 3.458,
      sell: 3.467,
      source: 'sunat',
    });
    expect(() => parseSunatExchangeRate('<html>Request Rejected</html>')).toThrow(
      ExchangeRateError,
    );
  });

  it('descarga con tiempo límite y explica los fallos', async () => {
    const ok = vi.fn(async () => new Response('31/10/2026|3.458|3.467|'));
    const source = createSunatExchangeRateSource({
      url: 'http://sunat.test/tc.txt',
      fetchImpl: ok,
    });
    expect(await source.fetchToday()).toMatchObject({ date: '2026-10-31', sell: 3.467 });
    expect(ok).toHaveBeenCalledWith('http://sunat.test/tc.txt', expect.any(Object));

    const down = createSunatExchangeRateSource({
      fetchImpl: async () => new Response('', { status: 503 }),
    });
    await expect(down.fetchToday()).rejects.toThrow(/SUNAT respondió 503/);
    const offline = createSunatExchangeRateSource({
      fetchImpl: async () => {
        throw new TypeError('fetch failed');
      },
    });
    await expect(offline.fetchToday()).rejects.toThrow(/No se pudo descargar.*fetch failed/);
  });

  it('valida fechas que existen', () => {
    expect(parseDate('5/1/2026')).toBe('2026-01-05');
    expect(parseDate('2026-10-31')).toBe('2026-10-31');
    expect(parseDate('31/02/2026')).toBeNull();
    expect(parseDate('2026-13-01')).toBeNull();
    expect(parseDate('ayer')).toBeNull();
  });

  it('carga un CSV con encabezado, separadores variados y solo venta', () => {
    const { rates, errors } = parseExchangeRateCsv(
      'Fecha;Compra;Venta\n01/10/2026;3.720;3.731\r\n2026-10-02|3.715|3.725\n\n03/10/2026\t3.730\n',
    );
    expect(errors).toEqual([]);
    expect(rates).toEqual([
      { currency: 'USD', date: '2026-10-01', buy: 3.72, sell: 3.731, source: 'csv' },
      { currency: 'USD', date: '2026-10-02', buy: 3.715, sell: 3.725, source: 'csv' },
      { currency: 'USD', date: '2026-10-03', buy: null, sell: 3.73, source: 'csv' },
    ]);
    expect(parseExchangeRateCsv('01/10/2026,4.10', { currency: 'EUR' }).rates[0]).toMatchObject({
      currency: 'EUR',
      sell: 4.1,
    });
  });

  it('informa cada línea con errores', () => {
    const { errors } = parseExchangeRateCsv('01/10/2026,3.7,3.8\n32/10/2026,3.7,3.8\n02/10/2026,x');
    expect(errors).toEqual(['Línea 2: fecha no válida', 'Línea 3: importes no válidos']);
  });
});
