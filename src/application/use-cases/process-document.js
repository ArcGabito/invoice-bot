import { businessKey } from '../../domain/duplicate-detection.js';
import { DOCUMENT_STATUS, SEVERITY, normalizeInvoice } from '../../domain/invoice.js';
import {
  VALIDATION_RULES_VERSION,
  statusFromIssues,
  validateInvoice,
} from '../../domain/invoice-validation.js';
import { DocumentReadError, ExtractionError } from '../../shared/errors.js';
import { PROMPT_VERSION } from '../../infrastructure/ai/prompt.js';

/** Reintentos inmediatos dentro de una misma ejecución cuando el JSON no es válido. */
const INVALID_OUTPUT_RETRIES = 1;

/**
 * Procesa un documento ya registrado: texto → IA → normalización →
 * validación → persistencia. Nunca lanza por un fallo del documento: lo
 * registra y devuelve el resultado, para que el lote continúe.
 *
 * @returns {Promise<{ outcome: 'extracted' | 'needs_ocr' | 'failed', validationStatus?: string, possibleDuplicate?: boolean, error?: string }>}
 */
export async function processDocument({ document, filePath, deps }) {
  const { repository, documentRouter, aiProvider, config, logger, today } = deps;
  const log = logger.child({ document_id: document.id, sha: document.sha256.slice(0, 12) });
  const attempt = repository.startAttempt(document.id);

  const fail = (kind, message) => {
    repository.updateDocument(document.id, {
      status: DOCUMENT_STATUS.FAILED,
      error_kind: kind,
      error_message: message,
    });
    log.warn({ kind }, 'documento fallido');
    return { outcome: 'failed', error: message, kind };
  };

  let extraction;
  try {
    extraction = await documentRouter.extractText({ filePath, mimeType: document.mime_type });
  } catch (error) {
    if (error instanceof DocumentReadError) return fail('unreadable', error.message);
    return fail('text_extraction', error.message);
  }

  repository.updateDocument(document.id, {
    text_method: extraction.method,
    text_chars: extraction.chars,
    page_count: extraction.pageCount,
  });
  if (extraction.needsOcr) {
    repository.updateDocument(document.id, { status: DOCUMENT_STATUS.NEEDS_OCR });
    log.info({ chars: extraction.chars }, 'sin texto suficiente: requiere OCR');
    return { outcome: 'needs_ocr' };
  }

  const baseRun = {
    document_id: document.id,
    attempt,
    provider: aiProvider.name,
    model: aiProvider.model,
    prompt_version: PROMPT_VERSION,
    text_method: extraction.method,
    input_chars: extraction.text.length,
  };

  let result;
  let runId;
  for (let tries = 0; ; tries++) {
    const started = Date.now();
    try {
      result = await aiProvider.extract({ text: extraction.text, fileName: document.file_name });
      runId = repository.insertExtractionRun({
        ...baseRun,
        model: result.model,
        status: 'success',
        raw_response: result.raw,
        input_tokens: result.usage?.input_tokens,
        output_tokens: result.usage?.output_tokens,
        duration_ms: Date.now() - started,
      });
      break;
    } catch (error) {
      const err =
        error instanceof ExtractionError
          ? error
          : new ExtractionError(error.message, { kind: 'unknown', cause: error });
      repository.insertExtractionRun({
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
      return fail(err.kind, err.message);
    }
  }

  if (!result.data.is_invoice) {
    return fail('not_invoice', 'El modelo indica que el documento no es un comprobante de pago');
  }

  const { invoice, normalizationIssues } = normalizeInvoice(result.data);
  const validation = validateInvoice(invoice, {
    normalizationIssues,
    toleranceCents: config.amountToleranceCents,
    today,
  });
  const issues = [...validation.issues];

  const key = businessKey(invoice);
  const matches = key ? repository.findByBusinessKey(key, document.id) : [];
  if (matches.length) {
    issues.push({
      rule_code: 'POSSIBLE_DUPLICATE',
      severity: SEVERITY.WARNING,
      message: 'Existe otro documento con el mismo emisor, tipo, serie y número',
      expected_value: null,
      actual_value: matches.map((m) => `#${m.document_id} ${m.file_name}`).join('; '),
    });
  }
  for (const note of result.data.notes ?? []) {
    issues.push({
      rule_code: 'MODEL_NOTE',
      severity: SEVERITY.INFO,
      message: note.slice(0, 500),
      expected_value: null,
      actual_value: null,
    });
  }

  const validationStatus = statusFromIssues(issues);
  repository.saveInvoiceResult({
    documentId: document.id,
    extractionRunId: runId,
    invoice,
    validationStatus,
    rulesVersion: VALIDATION_RULES_VERSION,
    businessKey: key,
    issues,
  });
  log.info({ validationStatus, issues: issues.length }, 'documento extraído');
  return { outcome: 'extracted', validationStatus, possibleDuplicate: matches.length > 0 };
}
