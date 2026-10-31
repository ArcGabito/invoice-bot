import { InvalidArgumentError } from 'commander';
import { hasSunatProblems, verifySunat } from '../../application/use-cases/verify-sunat.js';
import { EXIT_CODES } from '../../shared/errors.js';
import { formatDuration } from '../output.js';

export { hasSunatProblems };

function parsePositiveInt(value) {
  const n = Number(value);
  if (!Number.isInteger(n) || n < 1) throw new InvalidArgumentError('debe ser un entero positivo');
  return n;
}

export function registerVerifySunat(program, run) {
  program
    .command('verify-sunat')
    .description(
      'Consulta en SUNAT si cada comprobante es válido (aceptado, anulado, no existe) y el estado del RUC emisor',
    )
    .option('-m, --month <AAAA-MM>', 'solo comprobantes emitidos en ese periodo')
    .option('--force', 'vuelve a consultar también los ya verificados', false)
    .option('--limit <n>', 'máximo de consultas en esta ejecución', parsePositiveInt)
    .action((options) =>
      run(async (ctx, out) => {
        const summary = await verifySunat({
          deps: ctx,
          sunatClient: ctx.sunatClient,
          month: options.month ?? null,
          force: options.force,
          limit: options.limit ?? null,
          onProgress: sunatProgress(out),
        });
        out.data(summary);
        printSunatSummary(out, summary);
        return hasSunatProblems(summary) ? EXIT_CODES.PARTIAL : EXIT_CODES.OK;
      }),
    );
}

export const sunatProgress = (out) => (e) =>
  out.progress(`[${e.done}/${e.total}] ${e.invoice} → ${e.receiptStatus ?? `error: ${e.error}`}`);

export function printSunatSummary(out, summary) {
  out.line(`Comprobantes por consultar: ${summary.candidates}`);
  out.line(`Consultados: ${summary.checked}`);
  out.line(`  Aceptados o autorizados: ${summary.accepted}`);
  out.line(`  Anulados: ${summary.voided}`);
  out.line(`  No informados a SUNAT: ${summary.notFound}`);
  out.line(`  No autorizados: ${summary.notAuthorized}`);
  out.line(`Emisores con RUC no activo: ${summary.supplierNotActive}`);
  out.line(`Emisores no habidos o no hallados: ${summary.supplierNotHabido}`);
  out.line(`Consultas fallidas: ${summary.failed}`);
  for (const f of summary.failures) out.line(`  - ${f.invoice}: ${f.message}`);
  out.line(`Tiempo total: ${formatDuration(summary.durationMs)}`);
  if (summary.voided || summary.notFound || summary.notAuthorized) {
    out.line('Revisa el detalle con: invoicebot review');
  }
}
