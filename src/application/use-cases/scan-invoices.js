import path from 'node:path';
import { DOCUMENT_STATUS } from '../../domain/invoice.js';
import { mapWithConcurrency } from '../../shared/concurrency.js';
import { discoverFiles, hashFile } from '../../infrastructure/documents/file-scanner.js';
import { processDocument } from './process-document.js';

export function emptySummary() {
  return {
    found: 0,
    unsupported: [],
    queued: 0,
    extracted: 0,
    valid: 0,
    warning: 0,
    error: 0,
    failed: 0,
    needsOcr: 0,
    alreadyProcessed: 0,
    previouslyFailed: 0,
    exactDuplicates: 0,
    possibleDuplicates: 0,
    failures: [],
    durationMs: 0,
  };
}

export function addOutcome(summary, result, filePath) {
  if (result.outcome === 'extracted') {
    summary.extracted++;
    summary[result.validationStatus]++;
    if (result.possibleDuplicate) summary.possibleDuplicates++;
  } else if (result.outcome === 'needs_ocr') {
    summary.needsOcr++;
  } else {
    summary.failed++;
    summary.failures.push({ file: filePath, message: result.error });
  }
}

/**
 * Caso de uso HU-01: procesa los comprobantes de una carpeta.
 *
 * Fase 1 (secuencial): descubre archivos, calcula SHA-256 y registra cada
 * documento una sola vez. Así dos copias idénticas en el mismo lote no
 * compiten por insertar el mismo hash.
 * Fase 2 (concurrente, limitada): procesa los documentos pendientes.
 *
 * Es idempotente: un documento ya extraído no se vuelve a enviar a la IA.
 */
export async function scanInvoices({
  root,
  recursive = false,
  dryRun = false,
  deps,
  onProgress = () => {},
}) {
  const { repository, config, logger } = deps;
  const started = Date.now();
  const summary = emptySummary();

  const { supported, unsupported } = await discoverFiles(root, { recursive });
  summary.found = supported.length;
  summary.unsupported = unsupported;

  if (!dryRun) {
    const reset = repository.resetInterrupted();
    if (reset) logger.info({ reset }, 'documentos interrumpidos devueltos a pendiente');
  }

  const queue = [];
  const queuedIds = new Set();
  const seenInDryRun = new Set();
  for (const { filePath, mimeType } of supported) {
    let hash;
    try {
      hash = await hashFile(filePath);
    } catch (error) {
      summary.failed++;
      summary.failures.push({
        file: filePath,
        message: `No se pudo leer el archivo: ${error.message}`,
      });
      continue;
    }

    let document = repository.findDocumentBySha(hash.sha256);
    if (dryRun) {
      const enqueue = () => queue.push({ document, filePath, mimeType });
      if (document) classifyExisting(document, filePath, summary, enqueue);
      else if (seenInDryRun.has(hash.sha256)) summary.exactDuplicates++;
      else {
        seenInDryRun.add(hash.sha256);
        enqueue();
      }
      continue;
    }

    if (!document) {
      document = repository.insertDocument({
        sha256: hash.sha256,
        file_path: filePath,
        file_name: path.basename(filePath),
        mime_type: mimeType,
        file_size: hash.size,
      });
    }
    repository.recordOccurrence(document.id, filePath);
    classifyExisting(document, filePath, summary, () => {
      if (queuedIds.has(document.id)) return;
      queuedIds.add(document.id);
      queue.push({
        document,
        filePath: document.file_path === filePath ? filePath : document.file_path,
        mimeType,
      });
    });
  }

  summary.queued = queue.length;
  onProgress({ phase: 'discovered', summary });

  if (!dryRun) {
    let done = 0;
    await mapWithConcurrency(queue, config.concurrency, async (item) => {
      const result = await processDocument({
        document: item.document,
        filePath: item.filePath,
        deps,
      });
      addOutcome(summary, result, item.filePath);
      onProgress({
        phase: 'processed',
        done: ++done,
        total: queue.length,
        file: item.filePath,
        result,
      });
    });
  }

  summary.durationMs = Date.now() - started;
  return summary;
}

function classifyExisting(document, filePath, summary, enqueue) {
  const isCopy = document.file_path !== filePath;
  if (isCopy) summary.exactDuplicates++;
  switch (document.status) {
    case DOCUMENT_STATUS.EXTRACTED:
      if (!isCopy) summary.alreadyProcessed++;
      break;
    case DOCUMENT_STATUS.FAILED:
      if (!isCopy) summary.previouslyFailed++;
      break;
    case DOCUMENT_STATUS.NEEDS_OCR:
      if (!isCopy) summary.needsOcr++;
      break;
    default:
      // pending o processing: un documento nuevo o una ejecución interrumpida.
      enqueue?.();
  }
}
