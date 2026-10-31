import { findDuplicates } from '../../application/use-cases/find-duplicates.js';
import { getReviewQueue } from '../../application/use-cases/review-queue.js';
import { DOCUMENT_TYPES } from '../../domain/invoice.js';
import { EXIT_CODES } from '../../shared/errors.js';
import { invoiceLabel, money } from '../output.js';

const DOCUMENT_STATUS_LABEL = {
  pending: 'Pendientes',
  processing: 'En proceso',
  extracted: 'Extraídos',
  failed: 'Fallidos',
  needs_ocr: 'Requieren OCR',
  skipped: 'Omitidos (PDF vinculados a su XML, CDR)',
};

export function registerStatus(program, run) {
  program
    .command('status')
    .description('Muestra el estado de la base de datos y los documentos pendientes')
    .action(() =>
      run(async (ctx, out) => {
        const s = ctx.repository.getStatusSummary();
        const lastRuns = ctx.repository.listBotRuns(5);
        out.data({ database: ctx.config.dbPath, ...s, lastRuns });
        out.line(`Base de datos: ${ctx.config.dbPath}`);
        out.line();
        out.line('Documentos:');
        for (const [status, label] of Object.entries(DOCUMENT_STATUS_LABEL))
          out.line(`  ${label}: ${s.documents[status] ?? 0}`);
        out.line();
        out.line('Facturas:');
        out.line(`  Válidas: ${s.invoices.valid ?? 0}`);
        out.line(`  Con advertencias: ${s.invoices.warning ?? 0}`);
        out.line(`  Con errores: ${s.invoices.error ?? 0}`);
        out.line();
        out.line(`Duplicados exactos: ${s.exactDuplicates} · posibles: ${s.possibleDuplicates}`);
        out.line(
          `Extracciones con IA: ${s.runs.total} (fallidas: ${s.runs.failed ?? 0}) · tokens entrada/salida: ${s.runs.input_tokens}/${s.runs.output_tokens}`,
        );
        if (lastRuns.length) {
          out.line();
          out.line('Últimas ejecuciones del bot (invoicebot run):');
          for (const r of lastRuns) {
            const when = new Date(r.started_at).toLocaleString('es-PE');
            const detail = r.summary
              ? `${r.summary.found} documentos, ${r.summary.extracted} nuevos, ${r.summary.failed} fallidos`
              : (r.error_message ?? '');
            out.line(`  #${r.id} ${when} · ${r.trigger} · ${r.status} · ${detail}`);
          }
        }
        return EXIT_CODES.OK;
      }),
    );
}

export function registerDuplicates(program, run) {
  program
    .command('duplicates')
    .description('Lista duplicados exactos (mismo archivo) y posibles (mismo comprobante)')
    .action(() =>
      run(async (ctx, out) => {
        const { exact, possible } = findDuplicates({ deps: ctx });
        out.data({ exact, possible });
        out.line(`Duplicados exactos (mismo SHA-256): ${exact.length}`);
        for (const group of exact) {
          out.line(`  Documento #${group.key} (${group.items[0].sha256.slice(0, 12)}…)`);
          for (const item of group.items) out.line(`    - ${item.file_path}`);
        }
        out.line();
        out.line(`Posibles duplicados (mismo emisor, tipo, serie y número): ${possible.length}`);
        for (const group of possible) {
          const first = group.items[0];
          out.line(
            `  ${DOCUMENT_TYPES[first.document_type] ?? first.document_type} ${first.series}-${first.number} · RUC ${first.supplier_ruc}`,
          );
          for (const item of group.items) {
            out.line(
              `    - #${item.document_id} ${item.issue_date ?? 'sin fecha'} ${money(item.currency, item.total_cents)} · ${item.file_path}`,
            );
          }
        }
        if (exact.length || possible.length) {
          out.line();
          out.line('InvoiceBot no elimina documentos: revisa cada caso y decide cuál conservar.');
        }
        return EXIT_CODES.OK;
      }),
    );
}

export function registerReview(program, run) {
  program
    .command('review')
    .description(
      'Muestra facturas con errores o advertencias y documentos fallidos o pendientes de OCR',
    )
    .action(() =>
      run(async (ctx, out) => {
        const queue = getReviewQueue({ deps: ctx });
        out.data(queue);
        out.line(`Facturas por revisar: ${queue.invoices.length}`);
        for (const row of queue.invoices) {
          out.line(
            `  [${row.validation_status === 'error' ? 'ERROR' : 'AVISO'}] #${row.document_id} ${invoiceLabel(row)}`,
          );
          out.line(`      ${row.file_path}`);
          for (const i of row.issues) {
            const detail = [
              i.expected_value && `esperado ${i.expected_value}`,
              i.actual_value && `encontrado ${i.actual_value}`,
            ]
              .filter(Boolean)
              .join(', ');
            out.line(`      · ${i.rule_code}: ${i.message}${detail ? ` (${detail})` : ''}`);
          }
        }
        out.line();
        out.line(`Documentos fallidos: ${queue.failed.length}`);
        for (const d of queue.failed)
          out.line(
            `  #${d.id} ${d.file_path}\n      ${d.error_kind}: ${d.error_message} (intentos: ${d.attempts})`,
          );
        out.line();
        out.line(`Pendientes de OCR: ${queue.needsOcr.length}`);
        for (const d of queue.needsOcr) out.line(`  #${d.id} ${d.file_path}`);
        return EXIT_CODES.OK;
      }),
    );
}
