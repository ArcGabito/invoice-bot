import fs from 'node:fs';
import { Command, CommanderError, InvalidArgumentError } from 'commander';
import { EXIT_CODES, InvoiceBotError } from '../shared/errors.ts';
import { createContext } from './context.ts';
import type { Context, GlobalOptions } from './context.ts';
import { createPrinter } from './output.ts';
import type { Printer } from './output.ts';
import { registerDb } from './commands/db.ts';
import { registerScan } from './commands/scan.ts';
import { registerRetry } from './commands/retry.ts';
import { registerExport } from './commands/export.ts';
import { registerDuplicates, registerReview, registerStatus } from './commands/queries.ts';
import { registerConfig, registerDoctor } from './commands/diagnostics.ts';
import { registerVerifySunat } from './commands/verify-sunat.ts';
import { registerRun } from './commands/run.ts';
import { registerBackup } from './commands/backup.ts';
import { registerReviewActions } from './commands/review-actions.ts';
import { registerServe } from './commands/serve.ts';
import { registerUsers } from './commands/users.ts';

const pkg = JSON.parse(fs.readFileSync(new URL('../../package.json', import.meta.url), 'utf8')) as {
  version: string;
};

export type Action = (ctx: Context, out: Printer) => Promise<number>;

/**
 * Ejecuta la acción de un comando. Por defecto conecta antes con SQL Server
 * (y aplica las migraciones pendientes); `database: false` es para los
 * comandos que no la necesitan o la gestionan ellos mismos (config, doctor, db).
 */
export type Runner = (action: Action, options?: { database?: boolean }) => Promise<void>;

function parsePositiveInt(value: string): number {
  const n = Number(value);
  if (!Number.isInteger(n) || n < 1) throw new InvalidArgumentError('debe ser un entero positivo');
  return n;
}

export function buildProgram(): Command {
  const program = new Command();
  program
    .name('invoicebot')
    .description(
      'Bot RPA de comprobantes de pago: lectura de XML, PDF e imágenes, IA, validación contable, SUNAT y SQL Server',
    )
    .version(pkg.version, '-v, --version')
    .option('--data-dir <ruta>', 'directorio de datos locales (OCR)')
    .option('--provider <nombre>', 'proveedor de IA: anthropic, ollama o mock')
    .option('--concurrency <n>', 'documentos procesados en paralelo', parsePositiveInt)
    .option('--json', 'salida en JSON para automatización', false)
    .option('--verbose', 'muestra logs de progreso en stderr', false)
    .option(
      '--user <nombre>',
      'quién revisa (por defecto: INVOICEBOT_USER o el usuario de Windows)',
    )
    .showHelpAfterError()
    .exitOverride();

  /** Ejecuta la acción de un comando con su contexto y traduce errores a códigos de salida. */
  const run: Runner = async (action, { database = true } = {}) => {
    const globals = program.opts<GlobalOptions>();
    const out = createPrinter({ json: globals.json });
    let ctx: Context | undefined;
    try {
      ctx = createContext(globals);
      if (database) await ctx.connect();
      process.exitCode = await action(ctx, out);
    } catch (error) {
      process.exitCode = reportError(error, out);
    } finally {
      await ctx?.close();
    }
  };

  registerScan(program, run);
  registerStatus(program, run);
  registerExport(program, run);
  registerDuplicates(program, run);
  registerReview(program, run);
  registerRetry(program, run);
  registerDoctor(program, run);
  registerVerifySunat(program, run);
  registerRun(program, run);
  registerBackup(program, run);
  registerReviewActions(program, run);
  registerServe(program, run);
  registerUsers(program, run);
  registerConfig(program, run);
  registerDb(program, run);
  return program;
}

function reportError(error: unknown, out: Printer): number {
  const known = error instanceof InvoiceBotError;
  const err = error instanceof Error ? error : new Error(String(error));
  const code = (error as { code?: unknown }).code;
  if (out.json)
    process.stdout.write(
      `${JSON.stringify({ error: { code: typeof code === 'string' ? code : 'ERROR', message: err.message } })}\n`,
    );
  else process.stderr.write(`Error: ${err.message}\n`);
  if (!known && process.env.INVOICEBOT_DEBUG) process.stderr.write(`${err.stack}\n`);
  return known ? error.exitCode : EXIT_CODES.GENERAL_ERROR;
}

export async function main(argv: string[] = process.argv): Promise<void> {
  const program = buildProgram();
  try {
    await program.parseAsync(argv);
  } catch (error) {
    if (error instanceof CommanderError) {
      // --help y --version terminan con código 0; el resto son errores de uso.
      process.exitCode = error.exitCode === 0 ? EXIT_CODES.OK : EXIT_CODES.USAGE_ERROR;
      return;
    }
    throw error;
  }
}
