import fs from 'node:fs';
import { Command, CommanderError, InvalidArgumentError } from 'commander';
import { EXIT_CODES, InvoiceBotError } from '../shared/errors.js';
import { createContext } from './context.js';
import { createPrinter } from './output.js';
import { registerScan } from './commands/scan.js';
import { registerRetry } from './commands/retry.js';
import { registerExport } from './commands/export.js';
import { registerDuplicates, registerReview, registerStatus } from './commands/queries.js';
import { registerConfig, registerDoctor } from './commands/diagnostics.js';
import { registerVerifySunat } from './commands/verify-sunat.js';
import { registerRun } from './commands/run.js';
import { registerBackup } from './commands/backup.js';

const pkg = JSON.parse(fs.readFileSync(new URL('../../package.json', import.meta.url), 'utf8'));

function parsePositiveInt(value) {
  const n = Number(value);
  if (!Number.isInteger(n) || n < 1) throw new InvalidArgumentError('debe ser un entero positivo');
  return n;
}

export function buildProgram() {
  const program = new Command();
  program
    .name('invoicebot')
    .description(
      'Automatización local de facturas: extracción de texto, IA, validación contable y SQLite',
    )
    .version(pkg.version, '-v, --version')
    .option('--data-dir <ruta>', 'directorio de datos (base de datos SQLite)')
    .option('--provider <nombre>', 'proveedor de IA: anthropic, ollama o mock')
    .option('--concurrency <n>', 'documentos procesados en paralelo', parsePositiveInt)
    .option('--json', 'salida en JSON para automatización', false)
    .option('--verbose', 'muestra logs de progreso en stderr', false)
    .showHelpAfterError()
    .exitOverride();

  /** Ejecuta la acción de un comando con su contexto y traduce errores a códigos de salida. */
  const run = async (action) => {
    const globals = program.opts();
    const out = createPrinter({ json: globals.json });
    let ctx;
    try {
      ctx = createContext(globals);
      process.exitCode = await action(ctx, out);
    } catch (error) {
      process.exitCode = reportError(error, out);
    } finally {
      ctx?.close();
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
  registerConfig(program, run);
  return program;
}

function reportError(error, out) {
  const known = error instanceof InvoiceBotError;
  if (out.json)
    process.stdout.write(
      `${JSON.stringify({ error: { code: error.code ?? 'ERROR', message: error.message } })}\n`,
    );
  else process.stderr.write(`Error: ${error.message}\n`);
  if (!known && process.env.INVOICEBOT_DEBUG) process.stderr.write(`${error.stack}\n`);
  return known ? error.exitCode : EXIT_CODES.GENERAL_ERROR;
}

export async function main(argv = process.argv) {
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
