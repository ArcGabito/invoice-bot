import { businessKey } from '../../domain/duplicate-detection.js';
import {
  DATA_SOURCE,
  DOCUMENT_STATUS,
  SEVERITY,
  SKIP_REASON,
  hasInvoiceData,
  normalizeInvoice,
} from '../../domain/invoice.js';
import {
  VALIDATION_RULES_VERSION,
  statusFromIssues,
  validateInvoice,
} from '../../domain/invoice-validation.js';
import { DocumentReadError, ExtractionError } from '../../shared/errors.js';
import { PROMPT_VERSION } from '../../infrastructure/ai/prompt.js';
import { UBL_PARSER_VERSION } from '../../infrastructure/documents/ubl-parser.js';

/** Reintentos inmediatos dentro de una misma ejecución cuando el JSON no es válido. */
const INVALID_OUTPUT_RETRIES = 1;

/**
 * Procesa un documento ya registrado: datos (XML o texto + IA) →
 * normalización → validación → persistencia. Nunca lanza por un fallo del
 * documento: lo registra y devuelve el resultado, para que el lote continúe.
 *
 * @returns {Promise<{ outcome: 'extracted' | 'needs_ocr' | 'failed' | 'skipped', validationStatus?: string, possibleDuplicate?: boolean, error?: string, reason?: string }>}
 */
export async function processDocument({ document, filePath, deps }) {
  const { repository, documentRouter, config, logger, today } = deps;
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
  const skip = (reason, message) => {
    repository.updateDocument(document.id, {
      status: DOCUMENT_STATUS.SKIPPED,
      skip_reason: reason,
      error_message: message,
      processed_at: new Date().toISOString(),
    });
    log.info({ reason }, 'documento omitido');
    return { outcome: 'skipped', reason };
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

  let data;
  let runId;
  let dataSource;
  if (extraction.structured) {
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
    runId = repository.insertExtractionRun({
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
    const ai = await extractWithAi({ document, extraction, attempt, deps, log });
    if (ai.error) return fail(ai.error.kind, ai.error.message);
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
  const issues = [...validation.issues];
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
  for (const note of data.notes ?? []) {
    issues.push({
      rule_code: dataSource === DATA_SOURCE.XML ? 'XML_NOTE' : 'MODEL_NOTE',
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
    dataSource,
    issues,
  });
  log.info({ validationStatus, issues: issues.length, dataSource }, 'documento extraído');
  return {
    outcome: 'extracted',
    validationStatus,
    dataSource,
    possibleDuplicate: matches.length > 0,
  };
}

/**
 * Llama al proveedor de IA y registra cada intento. Repite una vez si la
 * salida no cumple el esquema; los demás errores se devuelven sin reintentar.
 */
async function extractWithAi({ document, extraction, attempt, deps, log }) {
  const { repository, aiProvider } = deps;
  const baseRun = {
    document_id: document.id,
    attempt,
    provider: aiProvider.name,
    model: aiProvider.model,
    prompt_version: PROMPT_VERSION,
    text_method: extraction.method,
    input_chars: extraction.text.length,
  };

  for (let tries = 0; ; tries++) {
    const started = Date.now();
    try {
      const result = await aiProvider.extract({
        text: extraction.text,
        fileName: document.file_name,
      });
      const runId = repository.insertExtractionRun({
        ...baseRun,
        model: result.model,
        status: 'success',
        raw_response: result.raw,
        input_tokens: result.usage?.input_tokens,
        output_tokens: result.usage?.output_tokens,
        duration_ms: Date.now() - started,
      });
      return { data: result.data, runId };
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
      return { error: { kind: err.kind, message: err.message } };
    }
  }
}
