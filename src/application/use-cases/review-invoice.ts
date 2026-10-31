import { businessKey } from '../../domain/duplicate-detection.ts';
import { SEVERITY } from '../../domain/invoice.ts';
import type { InvoiceData } from '../../domain/invoice.ts';
import { validateInvoice } from '../../domain/invoice-validation.ts';
import {
  CorrectionError,
  REVIEW_STATUS,
  SUNAT_RELEVANT_FIELDS,
  isPreservedOnRevalidation,
  parseCorrection,
  splitAssignment,
} from '../../domain/review.ts';
import type { Correction } from '../../domain/review.ts';
import { formatCents } from '../../shared/money.ts';
import { ReviewError } from '../../shared/errors.ts';
import type { Deps } from '../deps.ts';
import type { InvoiceRepository } from '../ports/invoice-repository.ts';
import type { InvoiceDetail } from '../../infrastructure/database/repositories/invoice-repository.ts';

/**
 * Casos de uso de la revisión humana. Cada acción se aplica en una
 * transacción junto con su registro en review_actions: no hay cambios sin
 * auditoría ni auditoría sin cambios.
 */

type ReviewDeps = Pick<Deps, 'repository'>;
type Id = number | string;

async function requireInvoice(
  repository: InvoiceRepository,
  invoiceId: Id,
): Promise<InvoiceDetail> {
  const id = Number(invoiceId);
  const invoice = Number.isSafeInteger(id) ? await repository.getInvoiceDetail(id) : null;
  if (!invoice) throw new ReviewError(`No existe la factura #${invoiceId}`);
  return invoice;
}

function requireUser(user: string | null | undefined): string {
  if (!user || !String(user).trim()) throw new ReviewError('Falta identificar quién revisa');
  return String(user).trim();
}

const openIssues = async (repository: InvoiceRepository, invoiceId: number) =>
  (await repository.getIssues(invoiceId)).filter((i) => !i.resolved_at);

/** Todo lo necesario para revisar una factura: datos, incidencias, historial y extracciones. */
export async function getInvoiceDetail({ deps, invoiceId }: { deps: ReviewDeps; invoiceId: Id }) {
  const { repository } = deps;
  const invoice = await requireInvoice(repository, invoiceId);
  return {
    invoice,
    issues: await repository.getIssues(invoice.id),
    actions: await repository.listReviewActions(invoice.id),
    extractionRuns: (await repository.listExtractionRuns(invoice.document_id)).map((r) => ({
      id: r.id,
      provider: r.provider,
      model: r.model,
      prompt_version: r.prompt_version,
      status: r.status,
      error_kind: r.error_kind,
      duration_ms: r.duration_ms,
      created_at: r.created_at,
    })),
  };
}

export type InvoiceDetailView = Awaited<ReturnType<typeof getInvoiceDetail>>;

/**
 * Aprueba una factura: una persona confirma que es correcta. Resuelve las
 * advertencias abiertas. Si quedan errores, exige `force` y una nota que
 * explique por qué se aprueba igual.
 */
export async function approveInvoice({
  deps,
  invoiceId,
  user,
  note = null,
  force = false,
}: {
  deps: ReviewDeps;
  invoiceId: Id;
  user: string | null | undefined;
  note?: string | null;
  force?: boolean;
}) {
  const { repository } = deps;
  const reviewer = requireUser(user);
  const invoice = await requireInvoice(repository, invoiceId);
  if (invoice.review_status === REVIEW_STATUS.REJECTED) {
    throw new ReviewError(`La factura #${invoice.id} está rechazada: reábrela antes de aprobarla`);
  }
  const errors = (await openIssues(repository, invoice.id)).filter(
    (i) => i.severity === SEVERITY.ERROR,
  );
  if (errors.length && !force) {
    throw new ReviewError(
      `La factura #${invoice.id} tiene ${errors.length} error(es) sin resolver (${errors
        .map((e) => e.rule_code)
        .join(', ')}): corrige los datos o aprueba con --force y una nota`,
    );
  }
  if (errors.length && !note?.trim()) {
    throw new ReviewError(
      'Para aprobar una factura con errores hace falta una nota que lo justifique',
    );
  }

  return repository.transaction(async (tx) => {
    const resolved = await tx.resolveOpenIssues(invoice.id, {
      severities: errors.length ? ['error', 'warning', 'info'] : ['warning', 'info'],
      user: reviewer,
      note: note?.trim() || 'Aprobada en revisión',
    });
    await tx.setReviewStatus(invoice.id, REVIEW_STATUS.APPROVED, reviewer);
    await tx.recordReviewAction({
      invoiceId: invoice.id,
      action: 'approve',
      user: reviewer,
      note: note?.trim() || null,
    });
    const validationStatus = await tx.recomputeValidationStatus(invoice.id);
    return {
      invoiceId: invoice.id,
      reviewStatus: REVIEW_STATUS.APPROVED,
      validationStatus,
      resolved,
    };
  });
}

/** Rechaza una factura: deja de exportarse y de aparecer en la bandeja. Exige un motivo. */
export async function rejectInvoice({
  deps,
  invoiceId,
  user,
  reason,
}: {
  deps: ReviewDeps;
  invoiceId: Id;
  user: string | null | undefined;
  reason?: string | null;
}) {
  const { repository } = deps;
  const reviewer = requireUser(user);
  const invoice = await requireInvoice(repository, invoiceId);
  const why = reason?.trim();
  if (!why) throw new ReviewError('Indica el motivo del rechazo');
  return repository.transaction(async (tx) => {
    await tx.setReviewStatus(invoice.id, REVIEW_STATUS.REJECTED, reviewer);
    await tx.recordReviewAction({
      invoiceId: invoice.id,
      action: 'reject',
      user: reviewer,
      note: why,
    });
    return { invoiceId: invoice.id, reviewStatus: REVIEW_STATUS.REJECTED };
  });
}

/** Devuelve una factura aprobada o rechazada a pendiente y reabre sus incidencias. */
export async function reopenInvoice({
  deps,
  invoiceId,
  user,
  note = null,
}: {
  deps: ReviewDeps;
  invoiceId: Id;
  user: string | null | undefined;
  note?: string | null;
}) {
  const { repository } = deps;
  const reviewer = requireUser(user);
  const invoice = await requireInvoice(repository, invoiceId);
  if (invoice.review_status === REVIEW_STATUS.PENDING) {
    throw new ReviewError(`La factura #${invoice.id} ya está pendiente de revisión`);
  }
  return repository.transaction(async (tx) => {
    const reopened = await tx.reopenIssues(invoice.id);
    await tx.setReviewStatus(invoice.id, REVIEW_STATUS.PENDING, reviewer);
    await tx.recordReviewAction({
      invoiceId: invoice.id,
      action: 'reopen',
      user: reviewer,
      note: note?.trim() || null,
    });
    const validationStatus = await tx.recomputeValidationStatus(invoice.id);
    return {
      invoiceId: invoice.id,
      reviewStatus: REVIEW_STATUS.PENDING,
      validationStatus,
      reopened,
    };
  });
}

/** Marca una incidencia concreta como resuelta, con una nota. */
export async function resolveIssue({
  deps,
  issueId,
  user,
  note = null,
}: {
  deps: ReviewDeps;
  issueId: Id;
  user: string | null | undefined;
  note?: string | null;
}) {
  const { repository } = deps;
  const reviewer = requireUser(user);
  const id = Number(issueId);
  const issue = Number.isSafeInteger(id) ? await repository.getIssue(id) : null;
  if (!issue) throw new ReviewError(`No existe la incidencia #${issueId}`);
  if (issue.resolved_at) throw new ReviewError(`La incidencia #${issueId} ya está resuelta`);
  if (issue.severity === SEVERITY.ERROR && !note?.trim()) {
    throw new ReviewError('Para dar por resuelto un error hace falta una nota que lo justifique');
  }
  return repository.transaction(async (tx) => {
    await tx.resolveIssue(issue.id, { user: reviewer, note: note?.trim() || null });
    await tx.recordReviewAction({
      invoiceId: issue.invoice_id,
      action: 'resolve_issue',
      user: reviewer,
      issueId: issue.id,
      field: issue.rule_code,
      note: note?.trim() || null,
    });
    const validationStatus = await tx.recomputeValidationStatus(issue.invoice_id);
    return { invoiceId: issue.invoice_id, issueId: issue.id, validationStatus };
  });
}

/**
 * Corrige datos de una factura y la revalida con las mismas reglas que la
 * extracción. `changes` acepta pares [campo, valor] o textos "campo=valor".
 * Cada cambio queda en el historial con su valor anterior y el nuevo.
 */
export async function correctInvoice({
  deps,
  invoiceId,
  changes,
  user,
  note = null,
}: {
  deps: Pick<Deps, 'repository' | 'config' | 'today'>;
  invoiceId: Id;
  changes: readonly (string | readonly [string, unknown])[];
  user: string | null | undefined;
  note?: string | null;
}) {
  const { repository, config, today } = deps;
  const reviewer = requireUser(user);
  const invoice = await requireInvoice(repository, invoiceId);
  if (invoice.review_status === REVIEW_STATUS.REJECTED) {
    throw new ReviewError(`La factura #${invoice.id} está rechazada: reábrela antes de corregirla`);
  }
  if (!changes?.length) throw new ReviewError('Indica al menos un cambio (campo=valor)');

  let parsed: Correction[];
  try {
    parsed = changes.map((c) => {
      const [field, value] = typeof c === 'string' ? splitAssignment(c) : c;
      return parseCorrection(field, value);
    });
  } catch (error) {
    if (error instanceof CorrectionError) throw new ReviewError(error.message, { cause: error });
    throw error;
  }
  const effective = parsed.filter((p) => (invoice[p.column] ?? null) !== p.value);
  if (effective.length === 0) {
    return {
      invoiceId: invoice.id,
      changed: [],
      validationStatus: invoice.validation_status,
      openIssues: await openIssues(repository, invoice.id),
    };
  }

  const display = (column: string, value: unknown) =>
    value === null || value === undefined
      ? ''
      : column.endsWith('_cents')
        ? formatCents(value as number)
        : String(value);

  return repository.transaction(async (tx) => {
    await tx.updateInvoiceFields(
      invoice.id,
      Object.fromEntries(effective.map((p) => [p.column, p.value])) as Partial<InvoiceData>,
    );
    for (const p of effective) {
      await tx.recordReviewAction({
        invoiceId: invoice.id,
        action: 'correct',
        user: reviewer,
        field: p.field,
        oldValue: display(p.column, invoice[p.column]),
        newValue: p.display,
        note: note?.trim() || null,
      });
    }
    if (effective.some((p) => SUNAT_RELEVANT_FIELDS.includes(p.field))) {
      await tx.clearSunatStatus(invoice.id);
    }

    const updated = await requireInvoice(tx, invoice.id);
    const { issues } = validateInvoice(updated, {
      toleranceCents: config.amountToleranceCents,
      today,
    });
    const key = businessKey(updated);
    if (key) await tx.lockBusinessKey(key);
    await tx.setBusinessKey(invoice.id, key);
    const matches = key ? await tx.findByBusinessKey(key, updated.document_id) : [];
    if (matches.length) {
      issues.push({
        rule_code: 'POSSIBLE_DUPLICATE',
        severity: SEVERITY.WARNING,
        message: 'Existe otro documento con el mismo emisor, tipo, serie y número',
        expected_value: null,
        actual_value: matches.map((m) => `#${m.document_id} ${m.file_name}`).join('; '),
      });
    }
    await tx.replaceRuleIssues(invoice.id, issues, isPreservedOnRevalidation);
    const validationStatus = await tx.recomputeValidationStatus(invoice.id);
    return {
      invoiceId: invoice.id,
      changed: effective.map((p) => ({
        field: p.field,
        from: display(p.column, invoice[p.column]),
        to: p.display,
      })),
      validationStatus,
      openIssues: await openIssues(tx, invoice.id),
    };
  });
}
