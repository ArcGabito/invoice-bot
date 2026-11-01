import fs from 'node:fs';
import type { Command } from 'commander';
import { saveExchangeRates } from '../../application/use-cases/exchange-rates.ts';
import type { ExchangeRateInput } from '../../application/use-cases/exchange-rates.ts';
import {
  exchangeRateSourceFromConfig,
  parseDate,
  parseExchangeRateCsv,
} from '../../infrastructure/fx/sunat-exchange-rate.ts';
import { ConfigError, EXIT_CODES } from '../../shared/errors.ts';
import type { Printer } from '../output.ts';
import type { Runner } from '../index.ts';

const CURRENCY = /^[A-Z]{3}$/;

function parseRate(value: string, what: string): number {
  const n = Number(value.replace(',', '.'));
  if (!(n > 0) || n > 1000) throw new ConfigError(`${what} no válido: "${value}"`);
  return n;
}

function parseCurrency(value: string): string {
  const currency = value.toUpperCase();
  if (!CURRENCY.test(currency) || currency === 'PEN') {
    throw new ConfigError(`Moneda no válida: "${value}" (usa un código ISO como USD o EUR)`);
  }
  return currency;
}

function printSaved(
  out: Printer,
  result: { saved: number; updated: number; missing: number },
  rates: ExchangeRateInput[],
): void {
  out.data({ ...result, rates });
  out.line(`Tipos de cambio guardados: ${result.saved} de ${rates.length}`);
  if (result.saved < rates.length) {
    out.line('  (los cargados a mano o por CSV no se reemplazan con la descarga de SUNAT)');
  }
  out.line(`Facturas actualizadas: ${result.updated}`);
  if (result.missing) out.line(`Facturas que siguen sin tipo de cambio: ${result.missing}`);
}

export function registerFx(program: Command, run: Runner): void {
  const fx = program
    .command('fx')
    .description('Tipo de cambio de SUNAT para registrar en soles los comprobantes en otra moneda');

  fx.command('fetch')
    .description('Descarga el tipo de cambio del dólar publicado hoy por SUNAT y lo aplica')
    .action(() =>
      run(async (ctx, out) => {
        const today = await exchangeRateSourceFromConfig(ctx.config).fetchToday();
        const rates = [today];
        printSaved(out, await saveExchangeRates({ rates, deps: ctx }), rates);
        out.line(
          `USD ${today.date}: compra ${today.buy?.toFixed(3)} · venta ${today.sell.toFixed(3)}`,
        );
        return EXIT_CODES.OK;
      }),
    );

  fx.command('set <fecha> <venta>')
    .description(
      'Registra a mano el tipo de cambio venta de un día (fecha DD/MM/AAAA o AAAA-MM-DD)',
    )
    .option('--compra <valor>', 'tipo de cambio compra (opcional)')
    .option('--moneda <código>', 'moneda (por defecto USD)', 'USD')
    .action((fecha: string, venta: string, options: { compra?: string; moneda: string }) =>
      run(async (ctx, out) => {
        const date = parseDate(fecha);
        if (!date) throw new ConfigError(`Fecha no válida: "${fecha}" (usa DD/MM/AAAA)`);
        const rates: ExchangeRateInput[] = [
          {
            currency: parseCurrency(options.moneda),
            date,
            buy: options.compra ? parseRate(options.compra, 'Tipo de cambio compra') : null,
            sell: parseRate(venta, 'Tipo de cambio venta'),
            source: 'manual',
          },
        ];
        printSaved(out, await saveExchangeRates({ rates, deps: ctx }), rates);
        return EXIT_CODES.OK;
      }),
    );

  fx.command('import <archivo>')
    .description(
      'Carga tipos de cambio de un CSV: fecha, compra, venta (una fila por día; encabezado opcional)',
    )
    .option('--moneda <código>', 'moneda (por defecto USD)', 'USD')
    .action((archivo: string, options: { moneda: string }) =>
      run(async (ctx, out) => {
        if (!fs.existsSync(archivo)) throw new ConfigError(`No existe el archivo ${archivo}`);
        const { rates, errors } = parseExchangeRateCsv(fs.readFileSync(archivo, 'utf8'), {
          currency: parseCurrency(options.moneda),
        });
        if (errors.length) {
          throw new ConfigError(
            `El archivo tiene errores; no se cargó nada:\n  ${errors.join('\n  ')}`,
          );
        }
        if (rates.length === 0) throw new ConfigError('El archivo no tiene tipos de cambio');
        printSaved(out, await saveExchangeRates({ rates, deps: ctx }), rates);
        return EXIT_CODES.OK;
      }),
    );

  fx.command('list')
    .description('Muestra los tipos de cambio guardados de un mes')
    .option('--month <AAAA-MM>', 'mes (por defecto el actual)')
    .option('--moneda <código>', 'moneda (por defecto USD)', 'USD')
    .action((options: { month?: string; moneda: string }) =>
      run(async (ctx, out) => {
        const month = options.month ?? ctx.today.slice(0, 7);
        if (!/^\d{4}-(0[1-9]|1[0-2])$/.test(month)) {
          throw new ConfigError(`Mes no válido: "${month}" (usa AAAA-MM)`);
        }
        const [y, m] = month.split('-').map(Number) as [number, number];
        const last = new Date(Date.UTC(y, m, 0)).getUTCDate();
        const rates = await ctx.repository.listExchangeRates({
          currency: parseCurrency(options.moneda),
          from: `${month}-01`,
          to: `${month}-${String(last).padStart(2, '0')}`,
        });
        out.data(rates);
        if (rates.length === 0) {
          out.line(`Sin tipos de cambio de ${options.moneda.toUpperCase()} en ${month}.`);
          return EXIT_CODES.OK;
        }
        out.line(`Fecha       Compra  Venta   Origen`);
        for (const r of rates) {
          out.line(
            `${r.rate_date}  ${r.buy === null ? '  -   ' : Number(r.buy).toFixed(3)}  ${Number(r.sell).toFixed(3)}  ${r.source}`,
          );
        }
        return EXIT_CODES.OK;
      }),
    );
}
