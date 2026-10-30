import fs from 'node:fs';
import path from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { scanInvoices } from '../../src/application/use-cases/scan-invoices.js';
import { verifySunat } from '../../src/application/use-cases/verify-sunat.js';
import { exportInvoices } from '../../src/application/use-cases/export-invoices.js';
import { SunatError } from '../../src/infrastructure/sunat/sunat-client.js';
import { ConfigError } from '../../src/shared/errors.js';
import { buildUblInvoice, writeUbl } from '../helpers/ubl-builder.js';
import { makeTestDeps } from '../helpers/context.js';

const opened = [];
afterEach(() => {
  for (const { db, dir } of opened.splice(0)) {
    db.close();
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

/** Registra tres facturas desde XML: F001-1, F001-2 y la boleta B001-3. */
async function setup() {
  const env = makeTestDeps();
  opened.push(env);
  const inbox = path.join(env.dir, 'facturas');
  fs.mkdirSync(inbox);
  writeUbl(path.join(inbox, 'a.xml'), buildUblInvoice({ id: 'F001-00000001' }));
  writeUbl(path.join(inbox, 'b.xml'), buildUblInvoice({ id: 'F001-00000002' }));
  writeUbl(
    path.join(inbox, 'c.xml'),
    buildUblInvoice({
      id: 'B001-00000003',
      typeCode: '03',
      customerId: '46779354',
      customerScheme: '1',
    }),
  );
  await scanInvoices({ root: inbox, deps: env.deps });
  return env;
}

/** Cliente SUNAT simulado: responde según el número del comprobante. */
function fakeSunat(byNumber) {
  return {
    validateReceipt: vi.fn(async ({ number }) => {
      const answer = byNumber[number];
      if (answer instanceof Error) throw answer;
      return {
        estadoRuc: '00',
        condDomiRuc: '00',
        observaciones: [],
        raw: { success: true },
        ...answer,
      };
    }),
  };
}

const invoiceBy = (db, number) => db.prepare('SELECT * FROM invoices WHERE number = ?').get(number);

describe('verifySunat', () => {
  it('guarda el estado de SUNAT, añade incidencias y recalcula la validación', async () => {
    const { deps, db } = await setup();
    const sunat = fakeSunat({
      '00000001': { estadoCp: '1' },
      '00000002': { estadoCp: '2', condDomiRuc: '12' },
      '00000003': { estadoCp: '0' },
    });

    const summary = await verifySunat({ deps, sunatClient: sunat });

    expect(summary).toMatchObject({
      candidates: 3,
      checked: 3,
      accepted: 1,
      voided: 1,
      notFound: 1,
      supplierNotHabido: 1,
      failed: 0,
    });
    expect(invoiceBy(db, '00000001')).toMatchObject({
      sunat_status: 'ACEPTADO',
      validation_status: 'valid',
    });
    expect(invoiceBy(db, '00000002')).toMatchObject({
      sunat_status: 'ANULADO',
      validation_status: 'error',
    });
    // Boleta aún no informada: advertencia, no error.
    expect(invoiceBy(db, '00000003')).toMatchObject({
      sunat_status: 'NO EXISTE',
      validation_status: 'warning',
    });

    const voided = invoiceBy(db, '00000002');
    expect(
      db
        .prepare('SELECT rule_code FROM validation_issues WHERE invoice_id = ? ORDER BY id')
        .pluck()
        .all(voided.id),
    ).toEqual(['SUNAT_RECEIPT_VOIDED', 'SUNAT_SUPPLIER_NOT_HABIDO']);
    expect(db.prepare('SELECT COUNT(*) FROM sunat_checks').pluck().get()).toBe(3);

    // La factura anulada sale del CSV por defecto (tiene error) y el estado se exporta.
    const result = exportInvoices({ month: '2026-10', deps });
    const csv = fs.readFileSync(result.path, 'utf8').trim().split('\r\n');
    expect(result.rows).toBe(2);
    expect(csv[0].endsWith(',data_source,sunat_status')).toBe(true);
    expect(csv.find((l) => l.includes('00000001'))).toMatch(/,xml,ACEPTADO$/);
  });

  it('sin --force solo vuelve a consultar lo no informado y reemplaza sus incidencias', async () => {
    const { deps, db } = await setup();
    await verifySunat({
      deps,
      sunatClient: fakeSunat({
        '00000001': { estadoCp: '1' },
        '00000002': { estadoCp: '1' },
        '00000003': { estadoCp: '0' },
      }),
    });

    const later = fakeSunat({ '00000003': { estadoCp: '1' } });
    const summary = await verifySunat({ deps, sunatClient: later });
    expect(later.validateReceipt).toHaveBeenCalledTimes(1);
    expect(summary).toMatchObject({ candidates: 1, accepted: 1 });
    const boleta = invoiceBy(db, '00000003');
    expect(boleta).toMatchObject({ sunat_status: 'ACEPTADO', validation_status: 'valid' });
    expect(
      db
        .prepare('SELECT COUNT(*) FROM validation_issues WHERE invoice_id = ?')
        .pluck()
        .get(boleta.id),
    ).toBe(0);

    const all = fakeSunat({
      '00000001': { estadoCp: '1' },
      '00000002': { estadoCp: '1' },
      '00000003': { estadoCp: '1' },
    });
    await verifySunat({ deps, sunatClient: all, force: true, limit: 2 });
    expect(all.validateReceipt).toHaveBeenCalledTimes(2);
  });

  it('registra un fallo puntual y sigue; lo vuelve a intentar en la próxima ejecución', async () => {
    const { deps, db } = await setup();
    const summary = await verifySunat({
      deps,
      sunatClient: fakeSunat({
        '00000001': new SunatError('Servicio no disponible', {
          kind: 'transient',
          retryable: true,
        }),
        '00000002': { estadoCp: '1' },
        '00000003': { estadoCp: '1' },
      }),
    });
    expect(summary).toMatchObject({ checked: 2, failed: 1 });
    expect(summary.failures[0]).toMatchObject({ invoice: 'F001-00000001' });
    expect(invoiceBy(db, '00000001').sunat_checked_at).toBeNull();
    expect(
      db.prepare("SELECT error_kind FROM sunat_checks WHERE status = 'failed'").pluck().get(),
    ).toBe('transient');

    const retry = fakeSunat({ '00000001': { estadoCp: '1' } });
    expect((await verifySunat({ deps, sunatClient: retry })).checked).toBe(1);
  });

  it('se detiene ante credenciales inválidas', async () => {
    const { deps } = await setup();
    const authError = new SunatError('SUNAT rechazó las credenciales', { kind: 'sunat_auth' });
    const sunat = fakeSunat({
      '00000001': authError,
      '00000002': authError,
      '00000003': authError,
    });
    await expect(verifySunat({ deps, sunatClient: sunat })).rejects.toBeInstanceOf(ConfigError);
    expect(sunat.validateReceipt).toHaveBeenCalledTimes(1);
  });
});
