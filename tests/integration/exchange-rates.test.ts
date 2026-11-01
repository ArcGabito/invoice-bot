import fs from 'node:fs';
import path from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import { scanInvoices } from '../../src/application/use-cases/scan-invoices.ts';
import { saveExchangeRates } from '../../src/application/use-cases/exchange-rates.ts';
import type { ExchangeRateInput } from '../../src/application/use-cases/exchange-rates.ts';
import { correctInvoice } from '../../src/application/use-cases/review-invoice.ts';
import { runBot } from '../../src/application/use-cases/run-bot.ts';
import { ExchangeRateError } from '../../src/infrastructure/fx/sunat-exchange-rate.ts';
import { buildUblInvoice, writeUbl } from '../helpers/ubl-builder.ts';
import { makeTestDeps } from '../helpers/context.ts';
import type { TestContext } from '../helpers/context.ts';
import { sqlColumn, sqlGet } from '../helpers/db.ts';

const opened: TestContext[] = [];
afterEach(async () => {
  for (const { dir } of opened.splice(0)) {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

const usd = (date: string, sell: number, source: ExchangeRateInput['source'] = 'manual') => ({
  currency: 'USD',
  date,
  buy: null,
  sell,
  source,
});

/** Servicio de USD 1,000 + IGV del jueves 15/10/2026, con detracción del 12 % en soles a 3.750. */
async function setup() {
  const env = await makeTestDeps();
  opened.push(env);
  const inbox = path.join(env.dir, 'in');
  fs.mkdirSync(inbox);
  writeUbl(
    path.join(inbox, 'usd.xml'),
    buildUblInvoice({
      currency: 'USD',
      taxes: [{ scheme: '1000', taxable: '1000.00', tax: '180.00' }],
      lineExtension: '1000.00',
      payable: '1180.00',
      detraction: { code: '037', percent: '12.00', amount: '531.00' },
    }),
  );
  return { ...env, inbox };
}

const issueCodes = (env: TestContext) =>
  sqlColumn<string>(
    env.db,
    'SELECT rule_code FROM validation_issues WHERE resolved_at IS NULL ORDER BY rule_code',
  );

describe('tipo de cambio en el flujo', () => {
  it('una factura en dólares espera el tipo de cambio y se completa al cargarlo', async () => {
    const env = await setup();
    await scanInvoices({ root: env.inbox, deps: env.deps });
    expect(await sqlGet(env.db, 'SELECT validation_status, exchange_rate FROM invoices')).toEqual({
      validation_status: 'warning',
      exchange_rate: null,
    });
    // Sin tipo de cambio no se puede comprobar el monto de la detracción.
    expect(await issueCodes(env)).toEqual(['FX_RATE_MISSING']);

    // Un tipo de cambio de hace más de 10 días no sirve.
    expect(await saveExchangeRates({ rates: [usd('2026-10-01', 3.7)], deps: env.deps })).toEqual({
      saved: 1,
      updated: 0,
      missing: 1,
    });
    // El del día anterior sí (p. ej. si el 15 no se publicó).
    expect(await saveExchangeRates({ rates: [usd('2026-10-14', 3.75)], deps: env.deps })).toEqual({
      saved: 1,
      updated: 1,
      missing: 0,
    });
    expect(
      await sqlGet(
        env.db,
        'SELECT validation_status, exchange_rate, exchange_rate_date, total_pen, detraction_amount FROM v_invoices',
      ),
    ).toEqual({
      validation_status: 'valid',
      exchange_rate: 3.75,
      exchange_rate_date: '2026-10-14',
      total_pen: 4425,
      detraction_amount: 531,
    });
    expect(await issueCodes(env)).toEqual([]);

    // Ya aplicado: un tipo de cambio posterior no lo cambia.
    expect(
      (await saveExchangeRates({ rates: [usd('2026-10-15', 3.8)], deps: env.deps })).updated,
    ).toBe(0);
  });

  it('al corregir la fecha de emisión busca el tipo de cambio de la nueva fecha', async () => {
    const env = await setup();
    await saveExchangeRates({
      rates: [usd('2026-10-15', 3.75), usd('2026-09-30', 3.5)],
      deps: env.deps,
    });
    await scanInvoices({ root: env.inbox, deps: env.deps });
    const { id } = await sqlGet(env.db, 'SELECT id FROM invoices');
    expect(await issueCodes(env)).toEqual([]);

    await correctInvoice({
      deps: env.deps,
      invoiceId: id,
      changes: ['issue_date=2026-09-30'],
      user: 'ana',
    });
    expect(await sqlGet(env.db, 'SELECT exchange_rate, exchange_rate_date FROM invoices')).toEqual({
      exchange_rate: 3.5,
      exchange_rate_date: '2026-09-30',
    });
    // A 3.500 el 12 % de 1,180 USD son S/ 495.60, no S/ 531.00.
    expect(await issueCodes(env)).toEqual(['DETRACTION_AMOUNT_MISMATCH']);
  });

  it('la descarga de SUNAT no reemplaza un tipo de cambio cargado a mano', async () => {
    const { deps } = await setup();
    await saveExchangeRates({ rates: [usd('2026-10-15', 3.75)], deps });
    expect(
      (await saveExchangeRates({ rates: [usd('2026-10-15', 3.8, 'sunat')], deps })).saved,
    ).toBe(0);
    await saveExchangeRates({ rates: [usd('2026-10-16', 3.8, 'sunat')], deps });
    await saveExchangeRates({ rates: [usd('2026-10-16', 3.81, 'sunat')], deps });
    const rates = await deps.repository.listExchangeRates({ from: '2026-10-01', to: '2026-10-31' });
    expect(rates.map((r) => [r.rate_date, Number(r.sell), r.source])).toEqual([
      ['2026-10-15', 3.75, 'manual'],
      ['2026-10-16', 3.81, 'sunat'],
    ]);
  });

  it('run descarga el tipo de cambio antes de escanear y un fallo no detiene el ciclo', async () => {
    const env = await setup();
    const ok = await runBot({
      deps: env.deps,
      inboxDir: env.inbox,
      exchangeRates: { fetchToday: async () => usd('2026-10-15', 3.75, 'sunat') },
    });
    expect(ok.status).toBe('success');
    expect(ok.report.fx).toEqual({ date: '2026-10-15', sell: 3.75, updated: 0 });
    expect(await sqlGet(env.db, 'SELECT validation_status, exchange_rate FROM invoices')).toEqual({
      validation_status: 'valid',
      exchange_rate: 3.75,
    });

    const down = await runBot({
      deps: env.deps,
      inboxDir: env.inbox,
      exchangeRates: {
        fetchToday: async () => {
          throw new ExchangeRateError('SUNAT respondió 503 al pedir el tipo de cambio');
        },
      },
    });
    expect(down.status).toBe('success');
    expect(down.report.fx).toEqual({ error: 'SUNAT respondió 503 al pedir el tipo de cambio' });
    const summary = await sqlGet(env.db, 'SELECT summary FROM bot_runs WHERE id = ?', down.runId);
    expect(JSON.parse(summary.summary)).toMatchObject({
      fxRate: null,
      fxError: expect.stringMatching(/503/),
    });
  });
});
