import { evaluateSunatResult } from '../../domain/sunat-status.js';
import { ConfigError } from '../../shared/errors.js';
import { SUPPORTED_DOCUMENT_TYPES } from '../../infrastructure/sunat/sunat-client.js';

/**
 * Consulta en SUNAT la validez de las facturas registradas y el estado del
 * RUC de su emisor. Las consultas son secuenciales para no saturar el
 * servicio. Un fallo puntual (red, rechazo de una consulta) se registra y el
 * lote continúa; credenciales inválidas detienen el lote, porque todas las
 * consultas siguientes fallarían igual.
 */
export async function verifySunat({
  deps,
  sunatClient,
  month = null,
  force = false,
  limit = null,
  onProgress = () => {},
}) {
  const { repository, logger } = deps;
  const started = Date.now();
  const summary = {
    candidates: 0,
    checked: 0,
    accepted: 0,
    voided: 0,
    notFound: 0,
    notAuthorized: 0,
    supplierNotActive: 0,
    supplierNotHabido: 0,
    failed: 0,
    failures: [],
    durationMs: 0,
  };

  const invoices = repository.listInvoicesForSunatCheck({
    month,
    documentTypes: [...SUPPORTED_DOCUMENT_TYPES],
    force,
    limit,
  });
  summary.candidates = invoices.length;

  for (const [index, invoice] of invoices.entries()) {
    const label = `${invoice.series}-${invoice.number}`;
    let outcome;
    try {
      const result = await sunatClient.validateReceipt({
        supplierRuc: invoice.supplier_ruc,
        documentType: invoice.document_type,
        series: invoice.series,
        number: invoice.number,
        issueDate: invoice.issue_date,
        totalCents: invoice.total_cents,
      });
      const { receiptStatus, issues } = evaluateSunatResult(result, {
        documentType: invoice.document_type,
      });
      const validationStatus = repository.recordSunatCheck({
        invoiceId: invoice.id,
        result,
        receiptStatus,
        issues,
      });
      summary.checked++;
      if (receiptStatus === 'ACEPTADO' || receiptStatus === 'AUTORIZADO') summary.accepted++;
      if (receiptStatus === 'ANULADO') summary.voided++;
      if (receiptStatus === 'NO EXISTE') summary.notFound++;
      if (receiptStatus === 'NO AUTORIZADO') summary.notAuthorized++;
      if (issues.some((i) => i.rule_code === 'SUNAT_SUPPLIER_NOT_ACTIVE'))
        summary.supplierNotActive++;
      if (issues.some((i) => i.rule_code === 'SUNAT_SUPPLIER_NOT_HABIDO'))
        summary.supplierNotHabido++;
      outcome = { receiptStatus, validationStatus };
    } catch (error) {
      const kind = error.kind ?? 'unknown';
      repository.recordSunatCheck({
        invoiceId: invoice.id,
        error: { kind, message: error.message },
      });
      summary.failed++;
      summary.failures.push({ invoice: label, message: error.message });
      logger.warn({ invoice_id: invoice.id, kind }, 'consulta a SUNAT fallida');
      if (kind === 'sunat_auth') throw new ConfigError(error.message, { cause: error });
      outcome = { error: error.message };
    }
    onProgress({ done: index + 1, total: invoices.length, invoice: label, ...outcome });
  }

  summary.durationMs = Date.now() - started;
  return summary;
}
