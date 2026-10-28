import { DOCUMENT_STATUS } from '../../domain/invoice.js';

/**
 * Documentos que necesitan atención: facturas con errores o advertencias,
 * documentos fallidos y documentos pendientes de OCR.
 */
export function getReviewQueue({ deps }) {
  const { repository } = deps;
  const invoices = repository.listInvoicesNeedingReview().map((row) => ({
    ...row,
    issues: repository
      .getIssues(row.invoice_id)
      .filter((i) => i.severity !== 'info' && !i.resolved_at),
  }));
  return {
    invoices,
    failed: repository.listDocumentsByStatus([DOCUMENT_STATUS.FAILED]),
    needsOcr: repository.listDocumentsByStatus([DOCUMENT_STATUS.NEEDS_OCR]),
  };
}
