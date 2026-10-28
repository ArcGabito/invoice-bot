import path from 'node:path';
import { retryDocuments } from '../../application/use-cases/retry-failed.js';
import { EXIT_CODES } from '../../shared/errors.js';
import { formatDuration } from '../output.js';

export function registerRetry(program, run) {
  program
    .command('retry')
    .description('Reintenta documentos fallidos y recupera los interrumpidos (sin flags: ambos)')
    .option('--failed', 'reintenta documentos fallidos')
    .option('--pending', 'recupera documentos pendientes o interrumpidos')
    .option('--force', 'ignora el límite de intentos y los errores no recuperables', false)
    .action((options) =>
      run(async (ctx, out) => {
        const both = !options.failed && !options.pending;
        void ctx.aiProvider; // valida la configuración del proveedor antes de empezar
        const summary = await retryDocuments({
          failed: both || Boolean(options.failed),
          pending: both || Boolean(options.pending),
          force: options.force,
          deps: ctx,
          onProgress: (e) =>
            out.progress(
              `[${e.done}/${e.total}] ${path.basename(e.file)} → ${e.result.validationStatus ?? e.result.outcome}`,
            ),
        });
        out.data(summary);
        out.line(`Candidatos: ${summary.found} · reintentados: ${summary.queued}`);
        out.line(
          `Extraídos: ${summary.extracted} (válidos ${summary.valid}, advertencias ${summary.warning}, errores ${summary.error})`,
        );
        out.line(`Fallidos: ${summary.failed} · requieren OCR: ${summary.needsOcr}`);
        for (const s of summary.skipped)
          out.line(`  omitido ${path.basename(s.file)}: ${s.reason}`);
        for (const f of summary.failures)
          out.line(`  error ${path.basename(f.file)}: ${f.message}`);
        out.line(`Tiempo total: ${formatDuration(summary.durationMs)}`);
        return summary.failed > 0 || summary.error > 0 ? EXIT_CODES.PARTIAL : EXIT_CODES.OK;
      }),
    );
}
