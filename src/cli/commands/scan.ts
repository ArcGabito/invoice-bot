import path from 'node:path';
import { scanInvoices } from '../../application/use-cases/scan-invoices.ts';
import { verifySunat } from '../../application/use-cases/verify-sunat.ts';
import { EXIT_CODES } from '../../shared/errors.ts';
import { formatDuration } from '../output.ts';
import { hasSunatProblems, printSunatSummary, sunatProgress } from './verify-sunat.ts';
import { describeDatabase } from '../../infrastructure/database/connection.ts';
import type { ScanSummary } from '../../application/use-cases/scan-invoices.ts';
import type { SunatSummary } from '../../application/use-cases/verify-sunat.ts';
import { errorMessage } from '../../shared/errors.ts';
import type { Printer } from '../output.ts';
import type { Command } from 'commander';
import type { Runner } from '../index.ts';

const OUTCOME_LABEL: Record<string, string> = {
  valid: 'válido',
  warning: 'con advertencias',
  error: 'con errores',
  needs_ocr: 'requiere OCR',
  failed: 'fallido',
  skipped: 'omitido',
  printed_representation: 'vinculado a su XML',
};

export function registerScan(program: Command, run: Runner): void {
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
    .action(
      (target: string, options: { recursive: boolean; dryRun: boolean; verifySunat?: boolean }) =>
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
                const r = event.result;
                const key =
                  r.outcome === 'extracted'
                    ? r.validationStatus
                    : r.outcome === 'skipped'
                      ? r.reason
                      : r.outcome;
                const label = OUTCOME_LABEL[key] ?? OUTCOME_LABEL[r.outcome];
                out.progress(
                  `[${event.done}/${event.total}] ${path.basename(event.file)} → ${label}`,
                );
              }
            },
          });
          let sunat: SunatSummary | { error: string } | null = null;
          if (sunatClient) {
            try {
              sunat = await verifySunat({ deps: ctx, sunatClient, onProgress: sunatProgress(out) });
            } catch (error) {
              // Lo escaneado ya está guardado; solo la verificación quedó incompleta.
              sunat = { error: errorMessage(error) };
            }
          }

          const database = describeDatabase(ctx.config);
          out.data({ dryRun: options.dryRun, ...summary, database, sunat });
          printSummary(out, summary, {
            dryRun: options.dryRun,
            database,
            version: program.version(),
          });
          if (sunat) {
            out.line();
            out.line('Verificación SUNAT');
            if ('error' in sunat) out.line(`  No se completó: ${sunat.error}`);
            else printSunatSummary(out, sunat);
          }
          const scanProblems = summary.failed > 0 || summary.error > 0;
          const sunatProblems = sunat !== null && ('error' in sunat || hasSunatProblems(sunat));
          return scanProblems || sunatProblems ? EXIT_CODES.PARTIAL : EXIT_CODES.OK;
        }),
    );
}

function printSummary(
  out: Printer,
  s: ScanSummary,
  { dryRun, database, version }: { dryRun: boolean; database: string; version: string | undefined },
): void {
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
  out.line(`Base de datos: SQL Server ${database}`);
}
