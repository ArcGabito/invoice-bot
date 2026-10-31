import path from 'node:path';
import { DATA_SOURCE, DOCUMENT_STATUS, SKIP_REASON } from '../../domain/invoice.ts';
import { XML_MIME_TYPE } from '../../infrastructure/documents/document-router.ts';
import { mapWithConcurrency } from '../../shared/concurrency.ts';
import { discoverFiles, hashFile } from '../../infrastructure/documents/file-scanner.ts';
import { processDocument } from './process-document.ts';
import { errorMessage } from '../../shared/errors.ts';
import type { ProcessResult } from './process-document.ts';
import type { Deps } from '../deps.ts';
import type { InvoiceRepository } from '../ports/invoice-repository.ts';
import type { DocumentRow } from '../../infrastructure/database/schema.ts';

export interface ScanSummary {
  found: number;
  unsupported: string[];
  queued: number;
  extracted: number;
  valid: number;
  warning: number;
  error: number;
  failed: number;
  needsOcr: number;
  fromXml: number;
  linkedToXml: number;
  skipped: number;
  alreadyProcessed: number;
  previouslyFailed: number;
  exactDuplicates: number;
  possibleDuplicates: number;
  failures: { file: string; message: string }[];
  durationMs: number;
}

export type ScanProgress =
  | { phase: 'discovered'; summary: ScanSummary }
  | { phase: 'processed'; done: number; total: number; file: string; result: ProcessResult };

export type ScanDeps = Pick<
  Deps,
  'repository' | 'documentRouter' | 'config' | 'logger' | 'today' | 'aiProvider' | 'visionExtractor'
>;

interface QueueItem {
  document: DocumentRow;
  filePath: string;
  mimeType: string;
}

export function emptySummary(): ScanSummary {
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
    fromXml: 0,
    linkedToXml: 0,
    skipped: 0,
    alreadyProcessed: 0,
    previouslyFailed: 0,
    exactDuplicates: 0,
    possibleDuplicates: 0,
    failures: [],
    durationMs: 0,
  };
}

export function addOutcome(summary: ScanSummary, result: ProcessResult, filePath: string): void {
  if (result.outcome === 'extracted') {
    summary.extracted++;
    const status = result.validationStatus as 'valid' | 'warning' | 'error';
    summary[status]++;
    if (result.dataSource === DATA_SOURCE.XML) summary.fromXml++;
    if (result.possibleDuplicate) summary.possibleDuplicates++;
  } else if (result.outcome === 'needs_ocr') {
    summary.needsOcr++;
  } else if (result.outcome === 'skipped') {
    if (result.reason === SKIP_REASON.PRINTED_REPRESENTATION) summary.linkedToXml++;
    else summary.skipped++;
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
 * Fase 2 (concurrente, limitada): procesa primero los XML y después el resto.
 * Un PDF con el mismo nombre que un XML ya extraído en su carpeta es su
 * representación impresa: se vincula al XML y no se envía a la IA.
 *
 * Es idempotente: un documento ya extraído no se vuelve a enviar a la IA.
 */
export async function scanInvoices({
  root,
  recursive = false,
  dryRun = false,
  deps,
  onProgress = () => {},
}: {
  root: string;
  recursive?: boolean;
  dryRun?: boolean;
  deps: ScanDeps;
  onProgress?: (event: ScanProgress) => void;
}): Promise<ScanSummary> {
  const { repository, config, logger } = deps;
  const ocrAvailable = Boolean(deps.documentRouter?.canOcr || deps.visionExtractor);
  const started = Date.now();
  const summary = emptySummary();

  const { supported, unsupported } = await discoverFiles(root, { recursive });
  summary.found = supported.length;
  summary.unsupported = unsupported;

  if (!dryRun) {
    const reset = await repository.resetInterrupted();
    if (reset) logger.info({ reset }, 'documentos interrumpidos devueltos a pendiente');
  }

  const queue: QueueItem[] = [];
  const queuedIds = new Set<number>();
  const seenInDryRun = new Set<string>();
  for (const { filePath, mimeType } of supported) {
    let hash: { sha256: string; size: number };
    try {
      hash = await hashFile(filePath);
    } catch (error) {
      summary.failed++;
      summary.failures.push({
        file: filePath,
        message: `No se pudo leer el archivo: ${errorMessage(error)}`,
      });
      continue;
    }

    const existing = await repository.findDocumentBySha(hash.sha256);
    if (dryRun) {
      // En simulación no hay documento registrado: solo cuenta lo que se procesaría.
      const enqueue = () => queue.push({ document: existing as DocumentRow, filePath, mimeType });
      if (existing) classifyExisting(existing, filePath, summary, enqueue, ocrAvailable);
      else if (seenInDryRun.has(hash.sha256)) summary.exactDuplicates++;
      else {
        seenInDryRun.add(hash.sha256);
        enqueue();
      }
      continue;
    }

    const document =
      existing ??
      (await repository.insertDocument({
        sha256: hash.sha256,
        file_path: filePath,
        file_name: path.basename(filePath),
        mime_type: mimeType,
        file_size: hash.size,
      }));
    await repository.recordOccurrence(document.id, filePath);
    classifyExisting(
      document,
      filePath,
      summary,
      () => {
        if (queuedIds.has(document.id)) return;
        queuedIds.add(document.id);
        queue.push({
          document,
          filePath: document.file_path === filePath ? filePath : document.file_path,
          mimeType,
        });
      },
      ocrAvailable,
    );
  }

  summary.queued = queue.length;
  onProgress({ phase: 'discovered', summary });

  if (!dryRun) {
    let done = 0;
    const run = async (item: QueueItem) => {
      const sibling =
        item.mimeType === XML_MIME_TYPE ? null : await findExtractedXmlSibling(item, repository);
      const result = sibling
        ? await linkToXml(item.document, sibling, repository)
        : await processDocument({ document: item.document, filePath: item.filePath, deps });
      addOutcome(summary, result, item.filePath);
      onProgress({
        phase: 'processed',
        done: ++done,
        total: queue.length,
        file: item.filePath,
        result,
      });
    };
    const xmlItems = queue.filter((item) => item.mimeType === XML_MIME_TYPE);
    const otherItems = queue.filter((item) => item.mimeType !== XML_MIME_TYPE);
    await mapWithConcurrency(xmlItems, config.concurrency, run);
    await mapWithConcurrency(otherItems, config.concurrency, run);
  }

  summary.durationMs = Date.now() - started;
  return summary;
}

/** XML extraído con el mismo nombre base y en la misma carpeta que el archivo. */
async function findExtractedXmlSibling(
  item: QueueItem,
  repository: InvoiceRepository,
): Promise<DocumentRow | null> {
  const { dir, name } = path.parse(item.filePath);
  for (const ext of ['.xml', '.XML']) {
    const xml = await repository.findDocumentByPath(path.join(dir, `${name}${ext}`));
    if (xml?.status === DOCUMENT_STATUS.EXTRACTED) return xml;
  }
  return null;
}

async function linkToXml(
  document: DocumentRow,
  xmlDocument: DocumentRow,
  repository: InvoiceRepository,
): Promise<ProcessResult> {
  await repository.updateDocument(document.id, {
    status: DOCUMENT_STATUS.SKIPPED,
    skip_reason: SKIP_REASON.PRINTED_REPRESENTATION,
    linked_document_id: xmlDocument.id,
    error_message: `Representación impresa de ${xmlDocument.file_name}: los datos se toman del XML`,
    processed_at: new Date().toISOString(),
  });
  return { outcome: 'skipped', reason: SKIP_REASON.PRINTED_REPRESENTATION };
}

/**
 * Clasifica un documento ya registrado. Con OCR disponible, los que quedaron
 * "requiere OCR" en ejecuciones anteriores se vuelven a procesar.
 */
function classifyExisting(
  document: DocumentRow,
  filePath: string,
  summary: ScanSummary,
  enqueue: () => void,
  ocrAvailable = false,
): void {
  const isCopy = document.file_path !== filePath;
  if (isCopy) summary.exactDuplicates++;
  switch (document.status) {
    case DOCUMENT_STATUS.EXTRACTED:
    case DOCUMENT_STATUS.SKIPPED:
      if (!isCopy) summary.alreadyProcessed++;
      break;
    case DOCUMENT_STATUS.FAILED:
      if (!isCopy) summary.previouslyFailed++;
      break;
    case DOCUMENT_STATUS.NEEDS_OCR:
      if (ocrAvailable && !isCopy) enqueue?.();
      else if (!isCopy) summary.needsOcr++;
      break;
    default:
      // pending o processing: un documento nuevo o una ejecución interrumpida.
      enqueue?.();
  }
}
