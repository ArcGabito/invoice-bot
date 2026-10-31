import fs from 'node:fs';
import path from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import { scanInvoices } from '../../src/application/use-cases/scan-invoices.js';
import { exportInvoices } from '../../src/application/use-cases/export-invoices.js';
import { findDuplicates } from '../../src/application/use-cases/find-duplicates.js';
import { getReviewQueue } from '../../src/application/use-cases/review-queue.js';
import {
  approveInvoice,
  correctInvoice,
  getInvoiceDetail,
  rejectInvoice,
  reopenInvoice,
  resolveIssue,
} from '../../src/application/use-cases/review-invoice.js';
import { parseCorrection, splitAssignment } from '../../src/domain/review.js';
import { ReviewError } from '../../src/shared/errors.js';
import { sampleInvoiceLines, writeTextPdf } from '../helpers/pdf-builder.js';
import { buildUblInvoice, writeUbl } from '../helpers/ubl-builder.js';
import { makeTestDeps } from '../helpers/context.js';

const opened = [];
afterEach(() => {
  for (const { db, dir } of opened.splice(0)) {
    db.close();
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

/** Una factura válida (XML), una con el total mal (error) y un duplicado (advertencia). */
async function setup() {
  const env = makeTestDeps({ env: { INVOICEBOT_CONCURRENCY: '1' } });
  opened.push(env);
  const inbox = path.join(env.dir, 'in');
  fs.mkdirSync(inbox);
  writeUbl(path.join(inbox, 'a-valida.xml'), buildUblInvoice({ id: 'F001-00000001' }));
  writeTextPdf(
    path.join(inbox, 'b-total-mal.pdf'),
    sampleInvoiceLines({ number: '00000002', total: '125.00' }),
  );
  writeTextPdf(path.join(inbox, 'c-original.pdf'), sampleInvoiceLines({ number: '00000003' }));
  writeTextPdf(path.join(inbox, 'd-copia.pdf'), [
    ...sampleInvoiceLines({ number: '00000003' }),
    'copia',
  ]);
  await scanInvoices({ root: inbox, deps: env.deps });
  const id = (file) =>
    env.db
      .prepare(
        'SELECT i.id FROM invoices i JOIN documents d ON d.id = i.document_id WHERE d.file_name = ?',
      )
      .pluck()
      .get(file);
  return { ...env, id };
}

const ANA = 'ana.contadora';

describe('parseCorrection', () => {
  it('normaliza los valores igual que la extracción', () => {
    expect(parseCorrection('total', 'S/ 1,180.50')).toMatchObject({
      column: 'total_cents',
      value: 118050,
      display: '1180.50',
    });
    expect(parseCorrection('issue_date', '15/10/2026')).toMatchObject({ value: '2026-10-15' });
    expect(parseCorrection('series', ' f001 ')).toMatchObject({ value: 'F001' });
    expect(parseCorrection('supplier_country', 'Estados Unidos')).toMatchObject({ value: 'US' });
    expect(parseCorrection('igv', '')).toMatchObject({ value: null });
  });

  it('rechaza campos desconocidos e importes ilegibles', () => {
    expect(() => parseCorrection('validation_status', 'valid')).toThrow(/No se puede corregir/);
    expect(() => parseCorrection('total', 'ciento dieciocho')).toThrow(/Importe no válido/);
    expect(splitAssignment('note=a=b')).toEqual(['note', 'a=b']);
    expect(() => splitAssignment('total')).toThrow(/campo=valor/);
  });
});

describe('revisión de facturas', () => {
  it('corregir revalida la factura y deja el cambio en el historial', async () => {
    const { deps, id } = await setup();
    const invoiceId = id('b-total-mal.pdf');
    expect(getInvoiceDetail({ deps, invoiceId }).invoice.validation_status).toBe('error');

    const result = correctInvoice({
      deps,
      invoiceId,
      changes: ['total=118.00'],
      user: ANA,
      note: 'Leído mal del PDF',
    });
    expect(result).toMatchObject({
      validationStatus: 'valid',
      changed: [{ field: 'total', from: '125.00', to: '118.00' }],
    });

    const detail = getInvoiceDetail({ deps, invoiceId });
    expect(detail.invoice).toMatchObject({ total_cents: 11800, corrected: 1 });
    expect(detail.issues.filter((i) => !i.resolved_at)).toEqual([]);
    expect(detail.actions).toEqual([
      expect.objectContaining({
        action: 'correct',
        field: 'total',
        old_value: '125.00',
        new_value: '118.00',
        user: ANA,
        note: 'Leído mal del PDF',
      }),
    ]);
    // Un valor igual al actual no genera cambios ni historial.
    expect(
      correctInvoice({ deps, invoiceId, changes: [['total', '118']], user: ANA }).changed,
    ).toEqual([]);
    expect(getInvoiceDetail({ deps, invoiceId }).actions).toHaveLength(1);
  });

  it('una corrección que cambia el número recalcula los posibles duplicados', async () => {
    const { deps, id } = await setup();
    const copy = id('d-copia.pdf');
    expect(getInvoiceDetail({ deps, invoiceId: copy }).issues.map((i) => i.rule_code)).toContain(
      'POSSIBLE_DUPLICATE',
    );
    const result = correctInvoice({
      deps,
      invoiceId: copy,
      changes: ['number=00000004'],
      user: ANA,
    });
    expect(result.validationStatus).toBe('valid');
    expect(findDuplicates({ deps }).possible).toEqual([]);
  });

  it('aprobar resuelve advertencias; con errores exige --force y una nota', async () => {
    const { deps, id } = await setup();
    const withError = id('b-total-mal.pdf');
    expect(() => approveInvoice({ deps, invoiceId: withError, user: ANA })).toThrow(
      /error\(es\) sin resolver/,
    );
    expect(() => approveInvoice({ deps, invoiceId: withError, user: ANA, force: true })).toThrow(
      /nota/,
    );

    const forced = approveInvoice({
      deps,
      invoiceId: withError,
      user: ANA,
      force: true,
      note: 'El proveedor confirmó el importe por correo',
    });
    expect(forced).toMatchObject({
      reviewStatus: 'approved',
      validationStatus: 'valid',
      resolved: 1,
    });
    const issue = getInvoiceDetail({ deps, invoiceId: withError }).issues[0];
    expect(issue).toMatchObject({
      resolved_by: ANA,
      resolution_note: 'El proveedor confirmó el importe por correo',
    });

    const duplicate = id('d-copia.pdf');
    expect(approveInvoice({ deps, invoiceId: duplicate, user: ANA })).toMatchObject({
      validationStatus: 'valid',
    });
    expect(getReviewQueue({ deps }).invoices).toEqual([]);
  });

  it('rechazar saca la factura de la bandeja, de los duplicados y de la exportación', async () => {
    const { deps, id } = await setup();
    const copy = id('d-copia.pdf');
    expect(() => rejectInvoice({ deps, invoiceId: copy, user: ANA })).toThrow(/motivo/);
    rejectInvoice({ deps, invoiceId: copy, user: ANA, reason: 'Duplicado de c-original.pdf' });

    expect(getReviewQueue({ deps }).invoices.map((i) => i.invoice_id)).not.toContain(copy);
    expect(findDuplicates({ deps }).possible).toEqual([]);
    const exported = await exportInvoices({
      month: '2026-10',
      includeErrors: true,
      includeDuplicates: true,
      deps,
    });
    expect(exported.rows).toBe(3);
    expect(() =>
      correctInvoice({ deps, invoiceId: copy, changes: ['total=1'], user: ANA }),
    ).toThrow(/rechazada/);

    // Reabrir la devuelve a la bandeja con sus incidencias.
    expect(reopenInvoice({ deps, invoiceId: copy, user: ANA, note: 'Me equivoqué' })).toMatchObject(
      { reviewStatus: 'pending' },
    );
    expect(getReviewQueue({ deps }).invoices.map((i) => i.invoice_id)).toContain(copy);
    expect(getInvoiceDetail({ deps, invoiceId: copy }).actions.map((a) => a.action)).toEqual([
      'reject',
      'reopen',
    ]);
  });

  it('resuelve una incidencia concreta; un error exige una nota', async () => {
    const { deps, id } = await setup();
    const invoiceId = id('b-total-mal.pdf');
    const [issue] = getInvoiceDetail({ deps, invoiceId }).issues;
    expect(() => resolveIssue({ deps, issueId: issue.id, user: ANA })).toThrow(ReviewError);
    expect(
      resolveIssue({
        deps,
        issueId: issue.id,
        user: ANA,
        note: 'Diferencia por redondeo del proveedor',
      }),
    ).toMatchObject({ validationStatus: 'valid' });
    expect(() => resolveIssue({ deps, issueId: issue.id, user: ANA, note: 'x' })).toThrow(
      /ya está resuelta/,
    );
  });

  it('exige quién revisa y una factura existente', async () => {
    const { deps, id } = await setup();
    expect(() => approveInvoice({ deps, invoiceId: id('a-valida.xml'), user: ' ' })).toThrow(
      /quién revisa/,
    );
    expect(() => getInvoiceDetail({ deps, invoiceId: 9999 })).toThrow(/No existe la factura #9999/);
  });
});
