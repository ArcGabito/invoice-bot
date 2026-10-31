import fs from 'node:fs';
import { DOCUMENT_STATUS } from '../../domain/invoice.ts';
import { mapWithConcurrency } from '../../shared/concurrency.ts';
import { hashFile } from '../../infrastructure/documents/file-scanner.ts';
import { processDocument } from './process-document.ts';
import { addOutcome, emptySummary } from './scan-invoices.ts';
import type { ScanDeps, ScanProgress, ScanSummary } from './scan-invoices.ts';
import type { InvoiceRepository } from '../ports/invoice-repository.ts';
import type { DocumentRow } from '../../infrastructure/database/schema.ts';

export type RetrySummary = Omit<ScanSummary, 'skipped'> & {
  skipped: { file: string; reason: string }[];
};

/**
 * Fallos que no se arreglan repitiendo la misma operación. Solo se reintentan
 * con --force (por ejemplo, tras cambiar de modelo o de proveedor).
 */
const PERMANENT_ERROR_KINDS = new Set([
  'not_invoice',
  'refused',
  'unreadable',
  'provider_auth',
  'provider_config',
  'file_missing',
  'file_changed',
]);

/**
 * Caso de uso HU-06: reintenta documentos fallidos y recupera los que
 * quedaron pendientes o en proceso por una ejecución interrumpida.
 */
export async function retryDocuments({
  failed = true,
  pending = true,
  force = false,
  deps,
  onProgress = () => {},
}: {
  failed?: boolean;
  pending?: boolean;
  force?: boolean;
  deps: ScanDeps;
  onProgress?: (event: ScanProgress) => void;
}): Promise<RetrySummary> {
  const { repository, config } = deps;
  const started = Date.now();
  const summary: RetrySummary = { ...emptySummary(), skipped: [] };

  await repository.resetInterrupted();
  const statuses: string[] = [];
  if (failed) statuses.push(DOCUMENT_STATUS.FAILED);
  if (pending) statuses.push(DOCUMENT_STATUS.PENDING);
  const candidates = statuses.length ? await repository.listDocumentsByStatus(statuses) : [];

  const queue: { document: DocumentRow; filePath: string }[] = [];
  for (const document of candidates) {
    if (document.status === DOCUMENT_STATUS.FAILED && !force) {
      if (document.attempts >= config.maxAttempts) {
        summary.skipped.push({
          file: document.file_path,
          reason: `alcanzó el máximo de ${config.maxAttempts} intentos`,
        });
        continue;
      }
      if (document.error_kind && PERMANENT_ERROR_KINDS.has(document.error_kind)) {
        summary.skipped.push({
          file: document.file_path,
          reason: `error no recuperable (${document.error_kind})`,
        });
        continue;
      }
    }
    const filePath = await locateFile(document, repository);
    if (!filePath.ok) {
      await repository.updateDocument(document.id, {
        status: DOCUMENT_STATUS.FAILED,
        error_kind: filePath.kind,
        error_message: filePath.message,
      });
      summary.failed++;
      summary.failures.push({ file: document.file_path, message: filePath.message });
      continue;
    }
    queue.push({ document, filePath: filePath.path });
  }

  summary.found = candidates.length;
  summary.queued = queue.length;
  let done = 0;
  await mapWithConcurrency(queue, config.concurrency, async (item) => {
    const result = await processDocument({
      document: item.document,
      filePath: item.filePath,
      deps,
    });
    addOutcome(summary as unknown as ScanSummary, result, item.filePath);
    onProgress({
      phase: 'processed',
      done: ++done,
      total: queue.length,
      file: item.filePath,
      result,
    });
  });

  summary.durationMs = Date.now() - started;
  return summary;
}

/** Busca una copia del archivo que siga existiendo y tenga el mismo SHA-256. */
async function locateFile(
  document: DocumentRow,
  repository: InvoiceRepository,
): Promise<{ ok: true; path: string } | { ok: false; kind: string; message: string }> {
  const paths = [
    document.file_path,
    ...(await repository.getOccurrences(document.id)).map((o) => o.file_path),
  ];
  let sawChanged = false;
  for (const candidate of new Set(paths)) {
    if (!fs.existsSync(candidate)) continue;
    const { sha256 } = await hashFile(candidate);
    if (sha256 === document.sha256) return { ok: true, path: candidate };
    sawChanged = true;
  }
  return sawChanged
    ? {
        ok: false,
        kind: 'file_changed',
        message: 'El archivo cambió de contenido desde que se registró',
      }
    : {
        ok: false,
        kind: 'file_missing',
        message: 'El archivo ya no existe en ninguna de las rutas registradas',
      };
}
