import path from 'node:path';
import { scanInvoices } from '../../application/use-cases/scan-invoices.js';
import { verifySunat } from '../../application/use-cases/verify-sunat.js';
import { EXIT_CODES } from '../../shared/errors.js';
import { formatDuration } from '../output.js';
import { hasSunatProblems, printSunatSummary, sunatProgress } from './verify-sunat.js';

const OUTCOME_LABEL = {
  valid: 'válido',
  warning: 'con advertencias',
  error: 'con errores',
  needs_ocr: 'requiere OCR',
  failed: 'fallido',
  skipped: 'omitido',
  printed_representation: 'vinculado a su XML',
};

export function registerScan(program, run) {
  program
    .command('scan')
    .description('Procesa los comprobantes (PDF, PNG, JPG) de una carpeta o un archivo')
    .argument('<ruta>', 'carpeta o archivo a procesar')
    .option('-r, --recursive', 'incluye subcarpetas', false)
    .option('--dry-run', 'inspecciona los archivos sin guardar nada ni llamar a la IA', false)
    .option(
      '--verify-sunat',
      'al terminar, verifica en SUNAT las facturas pendientes (por defecto: INVOICEBOT_SUNAT_VERIFY_ON_SCAN)',
    )
    .option('--no-verify-sunat', 'no verifica en SUNAT aunque esté configurado')
    .action((target, options) =>
      run(async (ctx, out) => {
        if (!options.dryRun) void ctx.aiProvider; // valida la configuración del proveedor antes de empezar
        // Si se pidió verificar, se comprueba la configuración de SUNAT antes de escanear.
        const verify = !options.dryRun && (options.verifySunat ?? ctx.config.sunat.verifyOnScan);
        const sunatClient = verify ? ctx.sunatClient : null;

        const summary = await scanInvoices({
          root: target,
          recursive: options.recursive,
          dryRun: options.dryRun,
          deps: ctx,
          onProgress: (event) => {
            if (event.phase === 'processed') {
              const { validationStatus, reason, outcome } = event.result;
              const label =
                OUTCOME_LABEL[validationStatus ?? reason ?? outcome] ?? OUTCOME_LABEL[outcome];
              out.progress(
                `[${event.done}/${event.total}] ${path.basename(event.file)} → ${label}`,
              );
            }
          },
        });
        let sunat = null;
        if (sunatClient) {
          try {
            sunat = await verifySunat({ deps: ctx, sunatClient, onProgress: sunatProgress(out) });
          } catch (error) {
            // Lo escaneado ya está guardado; solo la verificación quedó incompleta.
            sunat = { error: error.message };
          }
        }

        out.data({ dryRun: options.dryRun, ...summary, database: ctx.config.dbPath, sunat });
        printSummary(out, summary, {
          dryRun: options.dryRun,
          dbPath: ctx.config.dbPath,
          version: program.version(),
        });
        if (sunat) {
          out.line();
          out.line('Verificación SUNAT');
          if (sunat.error) out.line(`  No se completó: ${sunat.error}`);
          else printSunatSummary(out, sunat);
        }
        const scanProblems = summary.failed > 0 || summary.error > 0;
        const sunatProblems = sunat !== null && (Boolean(sunat.error) || hasSunatProblems(sunat));
        return scanProblems || sunatProblems ? EXIT_CODES.PARTIAL : EXIT_CODES.OK;
      }),
    );
}

function printSummary(out, s, { dryRun, dbPath, version }) {
  out.line(`InvoiceBot v${version}${dryRun ? ' (simulación: no se guardó nada)' : ''}`);
  out.line();
  out.line(`Documentos encontrados: ${s.found}`);
  if (dryRun) {
    out.line(`Nuevos por procesar: ${s.queued}`);
  } else {
    out.line(`Procesados correctamente: ${s.valid}`);
    out.line(`Con advertencias: ${s.warning}`);
    out.line(`Con errores: ${s.error}`);
    out.line(`Fallidos: ${s.failed}`);
    out.line(`Leídos de XML (sin IA): ${s.fromXml}`);
    if (s.linkedToXml) out.line(`PDF vinculados a su XML (sin IA): ${s.linkedToXml}`);
    if (s.skipped) out.line(`Omitidos (CDR u otros XML): ${s.skipped}`);
  }
  out.line(`Requieren OCR: ${s.needsOcr}`);
  out.line(`Ya procesados (omitidos): ${s.alreadyProcessed}`);
  if (s.previouslyFailed)
    out.line(`Fallidos anteriormente: ${s.previouslyFailed} (usa: invoicebot retry --failed)`);
  out.line(
    `Duplicados detectados: ${s.exactDuplicates + s.possibleDuplicates} (exactos: ${s.exactDuplicates}, posibles: ${s.possibleDuplicates})`,
  );
  if (s.unsupported.length) {
    out.line(`Archivos no compatibles omitidos: ${s.unsupported.length}`);
    for (const f of s.unsupported.slice(0, 10)) out.line(`  - ${f}`);
    if (s.unsupported.length > 10) out.line(`  … y ${s.unsupported.length - 10} más`);
  }
  if (s.failures.length) {
    out.line();
    out.line('Errores:');
    for (const f of s.failures) out.line(`  - ${path.basename(f.file)}: ${f.message}`);
  }
  out.line();
  out.line(`Tiempo total: ${formatDuration(s.durationMs)}`);
  out.line(`Base de datos: ${dbPath}`);
}
