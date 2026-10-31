import {
  approveInvoice,
  correctInvoice,
  getInvoiceDetail,
  rejectInvoice,
  reopenInvoice,
  resolveIssue,
} from '../../application/use-cases/review-invoice.js';
import { DOCUMENT_TYPES } from '../../domain/invoice.js';
import { formatCents } from '../../shared/money.js';
import { EXIT_CODES } from '../../shared/errors.js';

const REVIEW_LABEL = { pending: 'pendiente', approved: 'aprobada', rejected: 'rechazada' };
const ACTION_LABEL = {
  approve: 'aprobó',
  reject: 'rechazó',
  reopen: 'reabrió',
  correct: 'corrigió',
  resolve_issue: 'resolvió la incidencia',
};

const FIELD_ROWS = [
  ['Emisor', (i) => i.supplier_name],
  ['RUC emisor', (i) => i.supplier_ruc],
  ['ID fiscal (exterior)', (i) => i.supplier_tax_id],
  ['País emisor', (i) => i.supplier_country],
  ['Cliente', (i) => [i.customer_name, i.customer_ruc].filter(Boolean).join(' · ')],
  ['Tipo', (i) => i.document_type && `${i.document_type} ${DOCUMENT_TYPES[i.document_type] ?? ''}`],
  ['Serie-número', (i) => [i.series, i.number].filter(Boolean).join('-')],
  ['Emisión / vencimiento', (i) => [i.issue_date, i.due_date].filter(Boolean).join(' / ')],
  ['Moneda', (i) => i.currency],
];
const AMOUNT_ROWS = [
  ['Subtotal', 'subtotal_cents'],
  ['Op. gravadas', 'taxable_amount_cents'],
  ['Op. exoneradas', 'exempt_amount_cents'],
  ['Op. inafectas', 'unaffected_amount_cents'],
  ['Descuentos', 'discount_amount_cents'],
  ['IGV', 'igv_cents'],
  ['ISC', 'isc_cents'],
  ['ICBPER', 'icbper_cents'],
  ['Otros cargos', 'other_charges_cents'],
  ['Otros tributos', 'other_taxes_cents'],
  ['Anticipos', 'prepaid_amount_cents'],
  ['Total', 'total_cents'],
];

function printDetail(out, { invoice: i, issues, actions }) {
  out.line(
    `Factura #${i.id} · revisión: ${REVIEW_LABEL[i.review_status]}${i.reviewed_by ? ` (${i.reviewed_by})` : ''} · validación: ${i.validation_status}${i.corrected ? ' · corregida a mano' : ''}`,
  );
  out.line(`Archivo: ${i.file_path}`);
  out.line(`Origen: ${i.data_source}${i.sunat_status ? ` · SUNAT: ${i.sunat_status}` : ''}`);
  out.line();
  for (const [label, get] of FIELD_ROWS) out.line(`  ${label.padEnd(22)} ${get(i) || '—'}`);
  for (const [label, col] of AMOUNT_ROWS) {
    if (i[col] !== null && i[col] !== undefined) {
      out.line(`  ${label.padEnd(22)} ${formatCents(i[col]).padStart(12)}`);
    }
  }
  out.line();
  const open = issues.filter((x) => !x.resolved_at);
  const closed = issues.filter((x) => x.resolved_at);
  out.line(`Incidencias abiertas: ${open.length}`);
  for (const x of open) {
    const detail = [
      x.expected_value && `esperado ${x.expected_value}`,
      x.actual_value && `encontrado ${x.actual_value}`,
    ]
      .filter(Boolean)
      .join(', ');
    out.line(
      `  [#${x.id} ${x.severity}] ${x.rule_code}: ${x.message}${detail ? ` (${detail})` : ''}`,
    );
  }
  if (closed.length) {
    out.line(`Incidencias resueltas: ${closed.length}`);
    for (const x of closed) {
      out.line(
        `  [#${x.id}] ${x.rule_code} · ${x.resolved_by}${x.resolution_note ? `: ${x.resolution_note}` : ''}`,
      );
    }
  }
  if (actions.length) {
    out.line();
    out.line('Historial:');
    for (const a of actions) {
      const when = new Date(a.created_at).toLocaleString('es-PE');
      const what =
        a.action === 'correct'
          ? `${a.field}: "${a.old_value ?? ''}" → "${a.new_value ?? ''}"`
          : a.action === 'resolve_issue'
            ? `#${a.issue_id} ${a.field}`
            : '';
      out.line(
        `  ${when} · ${a.user} ${ACTION_LABEL[a.action]} ${what}${a.note ? ` — ${a.note}` : ''}`.trimEnd(),
      );
    }
  }
}

export function registerReviewActions(program, run) {
  program
    .command('show')
    .description('Muestra una factura con sus datos, incidencias e historial de revisión')
    .argument('<id>', 'número de factura (el #id que muestra review)')
    .action((id) =>
      run(async (ctx, out) => {
        const detail = getInvoiceDetail({ deps: ctx, invoiceId: id });
        out.data(detail);
        printDetail(out, detail);
        return EXIT_CODES.OK;
      }),
    );

  program
    .command('approve')
    .description('Aprueba una factura: confirma sus datos y resuelve sus advertencias')
    .argument('<id>', 'número de factura')
    .option('-n, --note <texto>', 'comentario (obligatorio con --force)')
    .option('--force', 'aprueba aunque tenga errores; exige una nota', false)
    .action((id, options) =>
      run(async (ctx, out) => {
        const result = approveInvoice({
          deps: ctx,
          invoiceId: id,
          user: ctx.reviewer,
          note: options.note,
          force: options.force,
        });
        out.data(result);
        out.line(
          `Factura #${result.invoiceId} aprobada por ${ctx.reviewer} (${result.resolved} incidencia(s) resueltas).`,
        );
        return EXIT_CODES.OK;
      }),
    );

  program
    .command('reject')
    .description('Rechaza una factura: deja de exportarse y de aparecer en la bandeja')
    .argument('<id>', 'número de factura')
    .requiredOption('-r, --reason <texto>', 'motivo del rechazo')
    .action((id, options) =>
      run(async (ctx, out) => {
        const result = rejectInvoice({
          deps: ctx,
          invoiceId: id,
          user: ctx.reviewer,
          reason: options.reason,
        });
        out.data(result);
        out.line(`Factura #${result.invoiceId} rechazada por ${ctx.reviewer}.`);
        return EXIT_CODES.OK;
      }),
    );

  program
    .command('reopen')
    .description('Devuelve una factura aprobada o rechazada a pendiente de revisión')
    .argument('<id>', 'número de factura')
    .option('-n, --note <texto>', 'comentario')
    .action((id, options) =>
      run(async (ctx, out) => {
        const result = reopenInvoice({
          deps: ctx,
          invoiceId: id,
          user: ctx.reviewer,
          note: options.note,
        });
        out.data(result);
        out.line(
          `Factura #${result.invoiceId} reabierta (${result.reopened} incidencia(s) vuelven a estar abiertas).`,
        );
        return EXIT_CODES.OK;
      }),
    );

  program
    .command('correct')
    .description(
      'Corrige datos de una factura y la revalida (p. ej. correct 12 total=118.00 issue_date=15/10/2026)',
    )
    .argument('<id>', 'número de factura')
    .argument('<cambios...>', 'uno o más campo=valor (valor vacío borra el dato)')
    .option('-n, --note <texto>', 'motivo de la corrección')
    .action((id, changes, options) =>
      run(async (ctx, out) => {
        const result = correctInvoice({
          deps: ctx,
          invoiceId: id,
          changes,
          user: ctx.reviewer,
          note: options.note,
        });
        out.data(result);
        if (!result.changed.length) {
          out.line('No hay cambios: los valores ya eran esos.');
          return EXIT_CODES.OK;
        }
        for (const c of result.changed) out.line(`  ${c.field}: "${c.from}" → "${c.to}"`);
        out.line(`Factura #${result.invoiceId} revalidada: ${result.validationStatus}.`);
        for (const x of result.openIssues.filter((i) => i.severity !== 'info')) {
          out.line(`  [#${x.id} ${x.severity}] ${x.rule_code}: ${x.message}`);
        }
        return EXIT_CODES.OK;
      }),
    );

  program
    .command('resolve')
    .description('Da por resuelta una incidencia concreta (el #id que muestra show)')
    .argument('<incidencia>', 'número de incidencia')
    .option('-n, --note <texto>', 'explicación (obligatoria para errores)')
    .action((issueId, options) =>
      run(async (ctx, out) => {
        const result = resolveIssue({ deps: ctx, issueId, user: ctx.reviewer, note: options.note });
        out.data(result);
        out.line(
          `Incidencia #${result.issueId} resuelta. Factura #${result.invoiceId}: ${result.validationStatus}.`,
        );
        return EXIT_CODES.OK;
      }),
    );
}
