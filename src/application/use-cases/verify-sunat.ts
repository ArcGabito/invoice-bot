import { evaluateSunatResult } from '../../domain/sunat-status.ts';
import { ConfigError } from '../../shared/errors.ts';
import { SUPPORTED_DOCUMENT_TYPES } from '../../infrastructure/sunat/sunat-client.ts';
import type { SunatClient } from '../../infrastructure/sunat/sunat-client.ts';
import { errorMessage } from '../../shared/errors.ts';
import type { Deps } from '../deps.ts';

export interface SunatSummary {
  candidates: number;
  checked: number;
  accepted: number;
  voided: number;
  notFound: number;
  notAuthorized: number;
  supplierNotActive: number;
  supplierNotHabido: number;
  failed: number;
  failures: { invoice: string; message: string }[];
  durationMs: number;
}

export interface SunatProgress {
  done: number;
  total: number;
  invoice: string;
  receiptStatus?: string | null;
  validationStatus?: string | null;
  error?: string;
}

/**
 * Consulta en SUNAT la validez de las facturas registradas y el estado del
 * RUC de su emisor. Las consultas son secuenciales para no saturar el
 * servicio. Un fallo puntual (red, rechazo de una consulta) se registra y el
 * lote continúa; credenciales inválidas detienen el lote, porque todas las
 * consultas siguientes fallarían igual.
 *
 * `delayMs` (INVOICEBOT_SUNAT_DELAY_MS) introduce una pausa entre consultas.
 */
export async function verifySunat({
  deps,
  sunatClient,
  month = null,
  force = false,
  limit = null,
  delayMs = deps.config?.sunat?.delayMs ?? 0,
  sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms)),
  onProgress = () => {},
}: {
  deps: Pick<Deps, 'repository' | 'logger' | 'config'>;
  sunatClient: Pick<SunatClient, 'validateReceipt'>;
  month?: string | null;
  force?: boolean;
  limit?: number | null;
  delayMs?: number;
  sleep?: (ms: number) => Promise<unknown>;
  onProgress?: (event: SunatProgress) => void;
}): Promise<SunatSummary> {
  const { repository, logger } = deps;
  const started = Date.now();
  const summary: SunatSummary = {
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

  const invoices = await repository.listInvoicesForSunatCheck({
    month,
    documentTypes: [...SUPPORTED_DOCUMENT_TYPES],
    force,
    limit,
  });
  summary.candidates = invoices.length;

  for (const [index, invoice] of invoices.entries()) {
    if (index > 0 && delayMs > 0) await sleep(delayMs);
    const label = `${invoice.series}-${invoice.number}`;
    let outcome: Omit<SunatProgress, 'done' | 'total' | 'invoice'>;
    try {
      // La consulta solo trae facturas con RUC, tipo, serie, número y fecha.
      const result = await sunatClient.validateReceipt({
        supplierRuc: invoice.supplier_ruc!,
        documentType: invoice.document_type!,
        series: invoice.series!,
        number: invoice.number!,
        issueDate: invoice.issue_date!,
        totalCents: invoice.total_cents,
      });
      const { receiptStatus, issues } = evaluateSunatResult(result, {
        documentType: invoice.document_type,
      });
      const validationStatus = await repository.recordSunatCheck({
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
      const kind = (error as { kind?: string }).kind ?? 'unknown';
      const message = errorMessage(error);
      await repository.recordSunatCheck({
        invoiceId: invoice.id,
        error: { kind, message },
      });
      summary.failed++;
      summary.failures.push({ invoice: label, message });
      logger.warn({ invoice_id: invoice.id, kind }, 'consulta a SUNAT fallida');
      if (kind === 'sunat_auth') throw new ConfigError(message, { cause: error });
      outcome = { error: message };
    }
    onProgress({ done: index + 1, total: invoices.length, invoice: label, ...outcome });
  }

  summary.durationMs = Date.now() - started;
  return summary;
}

/** Comprobantes anulados, no informados o no autorizados, o consultas fallidas. */
export function hasSunatProblems(summary: SunatSummary): boolean {
  return summary.failed + summary.voided + summary.notFound + summary.notAuthorized > 0;
}
