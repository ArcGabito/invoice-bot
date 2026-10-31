import { businessKey } from '../../domain/duplicate-detection.js';
import { SEVERITY } from '../../domain/invoice.js';
import { validateInvoice } from '../../domain/invoice-validation.js';
import {
  CorrectionError,
  REVIEW_STATUS,
  SUNAT_RELEVANT_FIELDS,
  isPreservedOnRevalidation,
  parseCorrection,
  splitAssignment,
} from '../../domain/review.js';
import { formatCents } from '../../shared/money.js';
import { ReviewError } from '../../shared/errors.js';

/**
 * Casos de uso de la revisión humana. Cada acción se aplica en una
 * transacción junto con su registro en review_actions: no hay cambios sin
 * auditoría ni auditoría sin cambios.
 */

function requireInvoice(repository, invoiceId) {
  const invoice = repository.getInvoiceDetail(Number(invoiceId));
  if (!invoice) throw new ReviewError(`No existe la factura #${invoiceId}`);
  return invoice;
}

function requireUser(user) {
  if (!user || !String(user).trim()) throw new ReviewError('Falta identificar quién revisa');
  return String(user).trim();
}

const openIssues = (repository, invoiceId) =>
  repository.getIssues(invoiceId).filter((i) => !i.resolved_at);

/** Todo lo necesario para revisar una factura: datos, incidencias, historial y extracciones. */
export function getInvoiceDetail({ deps, invoiceId }) {
  const { repository } = deps;
  const invoice = requireInvoice(repository, invoiceId);
  return {
    invoice,
    issues: repository.getIssues(invoice.id),
    actions: repository.listReviewActions(invoice.id),
    extractionRuns: repository.listExtractionRuns(invoice.document_id).map((r) => ({
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

/**
 * Aprueba una factura: una persona confirma que es correcta. Resuelve las
 * advertencias abiertas. Si quedan errores, exige `force` y una nota que
 * explique por qué se aprueba igual.
 */
export function approveInvoice({ deps, invoiceId, user, note = null, force = false }) {
  const { repository } = deps;
  const reviewer = requireUser(user);
  const invoice = requireInvoice(repository, invoiceId);
  if (invoice.review_status === REVIEW_STATUS.REJECTED) {
    throw new ReviewError(`La factura #${invoice.id} está rechazada: reábrela antes de aprobarla`);
  }
  const errors = openIssues(repository, invoice.id).filter((i) => i.severity === SEVERITY.ERROR);
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

  return repository.transaction(() => {
    const resolved = repository.resolveOpenIssues(invoice.id, {
      severities: errors.length ? ['error', 'warning', 'info'] : ['warning', 'info'],
      user: reviewer,
      note: note?.trim() || 'Aprobada en revisión',
    });
    repository.setReviewStatus(invoice.id, REVIEW_STATUS.APPROVED, reviewer);
    repository.recordReviewAction({
      invoiceId: invoice.id,
      action: 'approve',
      user: reviewer,
      note: note?.trim() || null,
    });
    const validationStatus = repository.recomputeValidationStatus(invoice.id);
    return {
      invoiceId: invoice.id,
      reviewStatus: REVIEW_STATUS.APPROVED,
      validationStatus,
      resolved,
    };
  });
}

/** Rechaza una factura: deja de exportarse y de aparecer en la bandeja. Exige un motivo. */
export function rejectInvoice({ deps, invoiceId, user, reason }) {
  const { repository } = deps;
  const reviewer = requireUser(user);
  const invoice = requireInvoice(repository, invoiceId);
  if (!reason?.trim()) throw new ReviewError('Indica el motivo del rechazo');
  return repository.transaction(() => {
    repository.setReviewStatus(invoice.id, REVIEW_STATUS.REJECTED, reviewer);
    repository.recordReviewAction({
      invoiceId: invoice.id,
      action: 'reject',
      user: reviewer,
      note: reason.trim(),
    });
    return { invoiceId: invoice.id, reviewStatus: REVIEW_STATUS.REJECTED };
  });
}

/** Devuelve una factura aprobada o rechazada a pendiente y reabre sus incidencias. */
export function reopenInvoice({ deps, invoiceId, user, note = null }) {
  const { repository } = deps;
  const reviewer = requireUser(user);
  const invoice = requireInvoice(repository, invoiceId);
  if (invoice.review_status === REVIEW_STATUS.PENDING) {
    throw new ReviewError(`La factura #${invoice.id} ya está pendiente de revisión`);
  }
  return repository.transaction(() => {
    const reopened = repository.reopenIssues(invoice.id);
    repository.setReviewStatus(invoice.id, REVIEW_STATUS.PENDING, reviewer);
    repository.recordReviewAction({
      invoiceId: invoice.id,
      action: 'reopen',
      user: reviewer,
      note: note?.trim() || null,
    });
    const validationStatus = repository.recomputeValidationStatus(invoice.id);
    return {
      invoiceId: invoice.id,
      reviewStatus: REVIEW_STATUS.PENDING,
      validationStatus,
      reopened,
    };
  });
}

/** Marca una incidencia concreta como resuelta, con una nota. */
export function resolveIssue({ deps, issueId, user, note = null }) {
  const { repository } = deps;
  const reviewer = requireUser(user);
  const issue = repository.getIssue(Number(issueId));
  if (!issue) throw new ReviewError(`No existe la incidencia #${issueId}`);
  if (issue.resolved_at) throw new ReviewError(`La incidencia #${issueId} ya está resuelta`);
  if (issue.severity === SEVERITY.ERROR && !note?.trim()) {
    throw new ReviewError('Para dar por resuelto un error hace falta una nota que lo justifique');
  }
  return repository.transaction(() => {
    repository.resolveIssue(issue.id, { user: reviewer, note: note?.trim() || null });
    repository.recordReviewAction({
      invoiceId: issue.invoice_id,
      action: 'resolve_issue',
      user: reviewer,
      issueId: issue.id,
      field: issue.rule_code,
      note: note?.trim() || null,
    });
    const validationStatus = repository.recomputeValidationStatus(issue.invoice_id);
    return { invoiceId: issue.invoice_id, issueId: issue.id, validationStatus };
  });
}

/**
 * Corrige datos de una factura y la revalida con las mismas reglas que la
 * extracción. `changes` acepta pares [campo, valor] o textos "campo=valor".
 * Cada cambio queda en el historial con su valor anterior y el nuevo.
 */
export function correctInvoice({ deps, invoiceId, changes, user, note = null }) {
  const { repository, config, today } = deps;
  const reviewer = requireUser(user);
  const invoice = requireInvoice(repository, invoiceId);
  if (invoice.review_status === REVIEW_STATUS.REJECTED) {
    throw new ReviewError(`La factura #${invoice.id} está rechazada: reábrela antes de corregirla`);
  }
  if (!changes?.length) throw new ReviewError('Indica al menos un cambio (campo=valor)');

  let parsed;
  try {
    parsed = changes.map((c) =>
      parseCorrection(...(typeof c === 'string' ? splitAssignment(c) : c)),
    );
  } catch (error) {
    if (error instanceof CorrectionError) throw new ReviewError(error.message, { cause: error });
    throw error;
  }
  const effective = parsed.filter((p) => (invoice[p.column] ?? null) !== p.value);
  if (effective.length === 0) {
    return { invoiceId: invoice.id, changed: [], validationStatus: invoice.validation_status };
  }

  const display = (column, value) =>
    value === null || value === undefined
      ? ''
      : column.endsWith('_cents')
        ? formatCents(value)
        : value;

  return repository.transaction(() => {
    repository.updateInvoiceFields(
      invoice.id,
      Object.fromEntries(effective.map((p) => [p.column, p.value])),
    );
    for (const p of effective) {
      repository.recordReviewAction({
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
      repository.clearSunatStatus(invoice.id);
    }

    const updated = repository.getInvoiceDetail(invoice.id);
    const { issues } = validateInvoice(updated, {
      toleranceCents: config.amountToleranceCents,
      today,
    });
    const key = businessKey(updated);
    repository.setBusinessKey(invoice.id, key);
    const matches = key ? repository.findByBusinessKey(key, updated.document_id) : [];
    if (matches.length) {
      issues.push({
        rule_code: 'POSSIBLE_DUPLICATE',
        severity: SEVERITY.WARNING,
        message: 'Existe otro documento con el mismo emisor, tipo, serie y número',
        expected_value: null,
        actual_value: matches.map((m) => `#${m.document_id} ${m.file_name}`).join('; '),
      });
    }
    repository.replaceRuleIssues(invoice.id, issues, isPreservedOnRevalidation);
    const validationStatus = repository.recomputeValidationStatus(invoice.id);
    return {
      invoiceId: invoice.id,
      changed: effective.map((p) => ({
        field: p.field,
        from: display(p.column, invoice[p.column]),
        to: p.display,
      })),
      validationStatus,
      openIssues: openIssues(repository, invoice.id),
    };
  });
}
