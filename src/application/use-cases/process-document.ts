import { businessKey } from '../../domain/duplicate-detection.ts';
import {
  DATA_SOURCE,
  DOCUMENT_STATUS,
  SEVERITY,
  SKIP_REASON,
  hasInvoiceData,
  normalizeInvoice,
} from '../../domain/invoice.ts';
import {
  VALIDATION_RULES_VERSION,
  statusFromIssues,
  validateInvoice,
} from '../../domain/invoice-validation.ts';
import { DocumentReadError, ExtractionError, errorMessage } from '../../shared/errors.ts';
import { PROMPT_VERSION } from '../../infrastructure/ai/prompt.ts';
import { UBL_PARSER_VERSION } from '../../infrastructure/documents/ubl-parser.ts';
import type { NewIssue, RawExtraction } from '../../domain/invoice.ts';
import type { DocumentRow, NewExtractionRun } from '../../infrastructure/database/schema.ts';
import type { AiExtractionResult } from '../ports/ai-provider.ts';
import type { TextExtraction } from '../ports/document-extractor.ts';
import type { Deps } from '../deps.ts';
import type { Config } from '../../shared/config.ts';
import type { Logger } from 'pino';

export type ProcessResult =
  | {
      outcome: 'extracted';
      validationStatus: string;
      dataSource: string;
      possibleDuplicate: boolean;
    }
  | { outcome: 'needs_ocr' }
  | { outcome: 'failed'; error: string; kind: string }
  | { outcome: 'skipped'; reason: string };

type ProcessDeps = Pick<
  Deps,
  'repository' | 'documentRouter' | 'config' | 'logger' | 'today' | 'aiProvider' | 'visionExtractor'
>;

/** Reintentos inmediatos dentro de una misma ejecución cuando el JSON no es válido. */
const INVALID_OUTPUT_RETRIES = 1;

/**
 * Procesa un documento ya registrado: datos (XML o texto + IA) →
 * normalización → validación → persistencia. Nunca lanza por un fallo del
 * documento: lo registra y devuelve el resultado, para que el lote continúe.
 */
export async function processDocument({
  document,
  filePath,
  deps,
}: {
  document: DocumentRow;
  filePath: string;
  deps: ProcessDeps;
}): Promise<ProcessResult> {
  const { repository, documentRouter, config, logger, today } = deps;
  const log = logger.child({ document_id: document.id, sha: document.sha256.slice(0, 12) });
  const attempt = await repository.startAttempt(document.id);

  const fail = async (kind: string, message: string): Promise<ProcessResult> => {
    await repository.updateDocument(document.id, {
      status: DOCUMENT_STATUS.FAILED,
      error_kind: kind,
      error_message: message,
    });
    log.warn({ kind }, 'documento fallido');
    return { outcome: 'failed', error: message, kind };
  };
  const skip = async (reason: string, message: string): Promise<ProcessResult> => {
    await repository.updateDocument(document.id, {
      status: DOCUMENT_STATUS.SKIPPED,
      skip_reason: reason,
      error_message: message,
      processed_at: new Date().toISOString(),
    });
    log.info({ reason }, 'documento omitido');
    return { outcome: 'skipped', reason };
  };

  let extraction: TextExtraction;
  try {
    extraction = await documentRouter.extractText({ filePath, mimeType: document.mime_type });
  } catch (error) {
    if (error instanceof DocumentReadError) return fail('unreadable', error.message);
    return fail('text_extraction', errorMessage(error));
  }

  // Sin texto legible: con visión de Claude, el documento se envía tal cual.
  const vision = extraction.needsOcr ? deps.visionExtractor : null;
  const useVision = vision !== null;
  await repository.updateDocument(document.id, {
    text_method: useVision ? 'ocr' : extraction.method,
    text_chars: extraction.chars,
    page_count: extraction.pageCount,
  });
  if (extraction.needsOcr && !useVision) {
    await repository.updateDocument(document.id, {
      status: DOCUMENT_STATUS.NEEDS_OCR,
      error_message: extraction.ocrAttempted
        ? 'El OCR no encontró texto legible: revisa la calidad de la imagen o del escaneo'
        : null,
    });
    log.info(
      { chars: extraction.chars, ocrAttempted: Boolean(extraction.ocrAttempted) },
      'sin texto suficiente: requiere OCR',
    );
    return { outcome: 'needs_ocr' };
  }

  let data: RawExtraction;
  let runId: number;
  let dataSource: string;
  if (vision) {
    const ai = await extractWithAi({
      document,
      attempt,
      deps,
      log,
      run: {
        provider: `${vision.name}-vision`,
        model: vision.model,
        prompt_version: PROMPT_VERSION,
        text_method: 'ocr',
      },
      call: () =>
        vision.extractFromFile({
          filePath,
          mimeType: document.mime_type,
          fileName: document.file_name,
        }),
    });
    if ('error' in ai) return fail(ai.error.kind, ai.error.message);
    ({ data, runId } = ai);
    dataSource = DATA_SOURCE.AI;
    if (!data.is_invoice && !hasInvoiceData(data)) {
      return fail('not_invoice', 'El modelo indica que el documento no es un comprobante de pago');
    }
  } else if (extraction.structured) {
    const { structured } = extraction;
    if (structured.kind === 'cdr') {
      return skip(
        SKIP_REASON.CDR,
        'Constancia de recepción (CDR) de SUNAT: acompaña al comprobante, no es uno',
      );
    }
    if (structured.kind === 'other') {
      return skip(
        SKIP_REASON.UNSUPPORTED_XML,
        `XML que no es un comprobante UBL (raíz: ${structured.root})`,
      );
    }
    data = structured.data;
    dataSource = DATA_SOURCE.XML;
    runId = await repository.insertExtractionRun({
      document_id: document.id,
      attempt,
      provider: 'ubl-xml',
      prompt_version: UBL_PARSER_VERSION,
      text_method: extraction.method,
      input_chars: extraction.chars,
      status: 'success',
      duration_ms: 0,
    });
  } else {
    const { aiProvider } = deps;
    const ai = await extractWithAi({
      document,
      attempt,
      deps,
      log,
      run: {
        provider: aiProvider.name,
        model: aiProvider.model,
        prompt_version: PROMPT_VERSION,
        text_method: extraction.method,
        input_chars: extraction.text?.length ?? 0,
      },
      call: () =>
        aiProvider.extract({
          text: extraction.text ?? '',
          fileName: document.file_name,
          ocr: extraction.method === 'ocr',
        }),
    });
    if ('error' in ai) return fail(ai.error.kind, ai.error.message);
    ({ data, runId } = ai);
    dataSource = DATA_SOURCE.AI;
    // La IA propone y el código decide: si el modelo duda pero entregó los
    // datos de un comprobante completo, se procesa y queda para revisión.
    if (!data.is_invoice && !hasInvoiceData(data)) {
      return fail('not_invoice', 'El modelo indica que el documento no es un comprobante de pago');
    }
  }

  const { invoice, normalizationIssues } = normalizeInvoice(data);
  const validation = validateInvoice(invoice, {
    normalizationIssues,
    toleranceCents: config.amountToleranceCents,
    today,
  });
  const issues: NewIssue[] = [
    ...validation.issues,
    ...ocrIssues({ extraction, useVision, config }),
  ];
  if (dataSource === DATA_SOURCE.AI && !data.is_invoice) {
    issues.push({
      rule_code: 'MODEL_DOUBTED_INVOICE',
      severity: SEVERITY.WARNING,
      message:
        'El modelo no reconoció el documento como comprobante, pero extrajo tipo, número, emisor y total: confirma que lo sea',
      expected_value: null,
      actual_value: null,
    });
  }

  const notes: NewIssue[] = ((data.notes as string[] | undefined) ?? []).map((note) => ({
    rule_code: dataSource === DATA_SOURCE.XML ? 'XML_NOTE' : 'MODEL_NOTE',
    severity: SEVERITY.INFO,
    message: note.slice(0, 500),
    expected_value: null,
    actual_value: null,
  }));

  // Buscar duplicados y guardar en la misma transacción, con la clave
  // reservada: si dos copias del mismo comprobante se procesan a la vez, la
  // segunda espera a que la primera quede guardada y la ve como duplicado.
  const key = businessKey(invoice);
  const { validationStatus, possibleDuplicate } = await repository.transaction(async (tx) => {
    if (key) await tx.lockBusinessKey(key);
    const matches = key ? await tx.findByBusinessKey(key, document.id) : [];
    const all: NewIssue[] = [...issues];
    if (matches.length) {
      all.push({
        rule_code: 'POSSIBLE_DUPLICATE',
        severity: SEVERITY.WARNING,
        message: 'Existe otro documento con el mismo emisor, tipo, serie y número',
        expected_value: null,
        actual_value: matches.map((m) => `#${m.document_id} ${m.file_name}`).join('; '),
      });
    }
    all.push(...notes);
    const status = statusFromIssues(all);
    await tx.saveInvoiceResult({
      documentId: document.id,
      extractionRunId: runId,
      invoice,
      validationStatus: status,
      rulesVersion: VALIDATION_RULES_VERSION,
      businessKey: key,
      dataSource,
      issues: all,
    });
    return { validationStatus: status, possibleDuplicate: matches.length > 0 };
  });
  log.info({ validationStatus, dataSource }, 'documento extraído');
  return {
    outcome: 'extracted',
    validationStatus,
    dataSource,
    possibleDuplicate,
  };
}

/**
 * Incidencias que dejan constancia de que los datos salieron de una imagen:
 * una nota informativa siempre y una advertencia si la confianza del OCR es
 * baja, para que una persona compare con el documento.
 */
function ocrIssues({
  extraction,
  useVision,
  config,
}: {
  extraction: TextExtraction;
  useVision: boolean;
  config: Readonly<Config>;
}): NewIssue[] {
  if (useVision) {
    return [
      {
        rule_code: 'OCR_SOURCE',
        severity: SEVERITY.INFO,
        message: 'Datos leídos de la imagen con visión (Claude), sin texto nativo',
        expected_value: null,
        actual_value: null,
      },
    ];
  }
  if (extraction.method !== 'ocr' || !extraction.ocr) return [];
  const { engine, confidence } = extraction.ocr;
  const issues: NewIssue[] = [
    {
      rule_code: 'OCR_SOURCE',
      severity: SEVERITY.INFO,
      message: `Datos leídos por OCR (${engine}) de una imagen o un escaneo`,
      expected_value: null,
      actual_value: `confianza ${confidence} %`,
    },
  ];
  if (confidence !== null && confidence < config.ocr.minConfidence) {
    issues.push({
      rule_code: 'OCR_LOW_CONFIDENCE',
      severity: SEVERITY.WARNING,
      message: 'El OCR leyó el documento con poca confianza: compara los datos con la imagen',
      expected_value: `>= ${config.ocr.minConfidence} %`,
      actual_value: `${confidence} %`,
    });
  }
  return issues;
}

/**
 * Llama a la IA (`call`) y registra cada intento en extraction_runs. Repite una
 * vez si la salida no cumple el esquema; los demás errores se devuelven sin reintentar.
 */
async function extractWithAi({
  document,
  attempt,
  deps,
  log,
  run,
  call,
}: {
  document: DocumentRow;
  attempt: number;
  deps: ProcessDeps;
  log: Logger;
  run: Omit<NewExtractionRun, 'document_id' | 'attempt' | 'status'>;
  call: () => Promise<AiExtractionResult>;
}): Promise<{ data: RawExtraction; runId: number } | { error: { kind: string; message: string } }> {
  const { repository } = deps;
  const baseRun = { document_id: document.id, attempt, ...run };

  for (let tries = 0; ; tries++) {
    const started = Date.now();
    try {
      const result = await call();
      const runId = await repository.insertExtractionRun({
        ...baseRun,
        model: result.model,
        status: 'success',
        raw_response: result.raw,
        input_tokens: result.usage?.input_tokens ?? null,
        output_tokens: result.usage?.output_tokens ?? null,
        duration_ms: Date.now() - started,
      });
      return { data: result.data, runId };
    } catch (error) {
      const err =
        error instanceof ExtractionError
          ? error
          : new ExtractionError(errorMessage(error), { kind: 'unknown', cause: error });
      await repository.insertExtractionRun({
        ...baseRun,
        status: 'failed',
        error_kind: err.kind,
        error_message: err.message,
        raw_response: err.rawResponse,
        duration_ms: Date.now() - started,
      });
      if (err.kind === 'invalid_output' && tries < INVALID_OUTPUT_RETRIES) {
        log.info('salida inválida, se repite la extracción');
        continue;
      }
      return { error: { kind: err.kind, message: err.message } };
    }
  }
}
