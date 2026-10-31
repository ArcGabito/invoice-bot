import fs from 'node:fs';
import path from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import { scanInvoices } from '../../src/application/use-cases/scan-invoices.ts';
import { exportInvoices } from '../../src/application/use-cases/export-invoices.ts';
import { findDuplicates } from '../../src/application/use-cases/find-duplicates.ts';
import { getReviewQueue } from '../../src/application/use-cases/review-queue.ts';
import {
  approveInvoice,
  correctInvoice,
  getInvoiceDetail,
  rejectInvoice,
  reopenInvoice,
  resolveIssue,
} from '../../src/application/use-cases/review-invoice.ts';
import { parseCorrection, splitAssignment } from '../../src/domain/review.ts';
import { ReviewError } from '../../src/shared/errors.ts';
import { sampleInvoiceLines, writeTextPdf } from '../helpers/pdf-builder.ts';
import { buildUblInvoice, writeUbl } from '../helpers/ubl-builder.ts';
import { makeTestDeps } from '../helpers/context.ts';
import type { TestContext } from '../helpers/context.ts';
import { sqlValue } from '../helpers/db.ts';

const opened: TestContext[] = [];
afterEach(async () => {
  for (const { dir } of opened.splice(0)) {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

/** Una factura válida (XML), una con el total mal (error) y un duplicado (advertencia). */
async function setup() {
  const env = await makeTestDeps({ env: { INVOICEBOT_CONCURRENCY: '1' } });
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
  const id = (file: string) =>
    sqlValue<number>(
      env.db,
      'SELECT i.id FROM invoices i JOIN documents d ON d.id = i.document_id WHERE d.file_name = ?',
      file,
    );
  return { ...env, id };
}

const ANA = 'ana.contadora';

describe('parseCorrection', () => {
  it('normaliza los valores igual que la extracción', async () => {
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

  it('rechaza campos desconocidos e importes ilegibles', async () => {
    expect(() => parseCorrection('validation_status', 'valid')).toThrow(/No se puede corregir/);
    expect(() => parseCorrection('total', 'ciento dieciocho')).toThrow(/Importe no válido/);
    expect(splitAssignment('note=a=b')).toEqual(['note', 'a=b']);
    expect(() => splitAssignment('total')).toThrow(/campo=valor/);
  });
});

describe('revisión de facturas', () => {
  it('corregir revalida la factura y deja el cambio en el historial', async () => {
    const { deps, id } = await setup();
    const invoiceId = await id('b-total-mal.pdf');
    expect((await getInvoiceDetail({ deps, invoiceId })).invoice.validation_status).toBe('error');

    const result = await correctInvoice({
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

    const detail = await getInvoiceDetail({ deps, invoiceId });
    expect(detail.invoice).toMatchObject({ total_cents: 11800, corrected: true });
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
      (await correctInvoice({ deps, invoiceId, changes: [['total', '118']], user: ANA })).changed,
    ).toEqual([]);
    expect((await getInvoiceDetail({ deps, invoiceId })).actions).toHaveLength(1);
  });

  it('una corrección que cambia el número recalcula los posibles duplicados', async () => {
    const { deps, id } = await setup();
    const copy = await id('d-copia.pdf');
    expect(
      (await getInvoiceDetail({ deps, invoiceId: copy })).issues.map((i) => i.rule_code),
    ).toContain('POSSIBLE_DUPLICATE');
    const result = await correctInvoice({
      deps,
      invoiceId: copy,
      changes: ['number=00000004'],
      user: ANA,
    });
    expect(result.validationStatus).toBe('valid');
    expect((await findDuplicates({ deps })).possible).toEqual([]);
  });

  it('aprobar resuelve advertencias; con errores exige --force y una nota', async () => {
    const { deps, id } = await setup();
    const withError = await id('b-total-mal.pdf');
    await expect(approveInvoice({ deps, invoiceId: withError, user: ANA })).rejects.toThrow(
      /error\(es\) sin resolver/,
    );
    await expect(
      approveInvoice({ deps, invoiceId: withError, user: ANA, force: true }),
    ).rejects.toThrow(/nota/);

    const forced = await approveInvoice({
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
    const issue = (await getInvoiceDetail({ deps, invoiceId: withError })).issues[0];
    expect(issue).toMatchObject({
      resolved_by: ANA,
      resolution_note: 'El proveedor confirmó el importe por correo',
    });

    const duplicate = await id('d-copia.pdf');
    expect(await approveInvoice({ deps, invoiceId: duplicate, user: ANA })).toMatchObject({
      validationStatus: 'valid',
    });
    expect((await getReviewQueue({ deps })).invoices).toEqual([]);
  });

  it('rechazar saca la factura de la bandeja, de los duplicados y de la exportación', async () => {
    const { deps, id } = await setup();
    const copy = await id('d-copia.pdf');
    await expect(rejectInvoice({ deps, invoiceId: copy, user: ANA })).rejects.toThrow(/motivo/);
    await rejectInvoice({
      deps,
      invoiceId: copy,
      user: ANA,
      reason: 'Duplicado de c-original.pdf',
    });

    expect((await getReviewQueue({ deps })).invoices.map((i) => i.invoice_id)).not.toContain(copy);
    expect((await findDuplicates({ deps })).possible).toEqual([]);
    const exported = await exportInvoices({
      month: '2026-10',
      includeErrors: true,
      includeDuplicates: true,
      deps,
    });
    expect(exported.rows).toBe(3);
    await expect(
      correctInvoice({ deps, invoiceId: copy, changes: ['total=1'], user: ANA }),
    ).rejects.toThrow(/rechazada/);

    // Reabrir la devuelve a la bandeja con sus incidencias.
    expect(
      await reopenInvoice({ deps, invoiceId: copy, user: ANA, note: 'Me equivoqué' }),
    ).toMatchObject({ reviewStatus: 'pending' });
    expect((await getReviewQueue({ deps })).invoices.map((i) => i.invoice_id)).toContain(copy);
    expect(
      (await getInvoiceDetail({ deps, invoiceId: copy })).actions.map((a) => a.action),
    ).toEqual(['reject', 'reopen']);
  });

  it('resuelve una incidencia concreta; un error exige una nota', async () => {
    const { deps, id } = await setup();
    const invoiceId = await id('b-total-mal.pdf');
    const [issue] = (await getInvoiceDetail({ deps, invoiceId })).issues;
    await expect(resolveIssue({ deps, issueId: issue.id, user: ANA })).rejects.toThrow(ReviewError);
    expect(
      await resolveIssue({
        deps,
        issueId: issue.id,
        user: ANA,
        note: 'Diferencia por redondeo del proveedor',
      }),
    ).toMatchObject({ validationStatus: 'valid' });
    await expect(resolveIssue({ deps, issueId: issue.id, user: ANA, note: 'x' })).rejects.toThrow(
      /ya está resuelta/,
    );
  });

  it('exige quién revisa y una factura existente', async () => {
    const { deps, id } = await setup();
    await expect(
      approveInvoice({ deps, invoiceId: await id('a-valida.xml'), user: ' ' }),
    ).rejects.toThrow(/quién revisa/);
    await expect(getInvoiceDetail({ deps, invoiceId: 9999 })).rejects.toThrow(
      /No existe la factura #9999/,
    );
  });
});
