import { DOCUMENT_STATUS } from '../../domain/invoice.ts';
import type { Deps } from '../deps.ts';

/**
 * Documentos que necesitan atención: facturas con errores o advertencias,
 * documentos fallidos y documentos pendientes de OCR.
 */
export async function getReviewQueue({ deps }: { deps: Pick<Deps, 'repository'> }) {
  const { repository } = deps;
  const invoices = await Promise.all(
    (await repository.listInvoicesNeedingReview()).map(async (row) => ({
      ...row,
      issues: (await repository.getIssues(row.invoice_id)).filter(
        (i) => i.severity !== 'info' && !i.resolved_at,
      ),
    })),
  );
  return {
    invoices,
    failed: await repository.listDocumentsByStatus([DOCUMENT_STATUS.FAILED]),
    needsOcr: await repository.listDocumentsByStatus([DOCUMENT_STATUS.NEEDS_OCR]),
  };
}

export type ReviewQueue = Awaited<ReturnType<typeof getReviewQueue>>;
