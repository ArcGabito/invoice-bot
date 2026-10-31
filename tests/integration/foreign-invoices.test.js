import fs from 'node:fs';
import path from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { scanInvoices } from '../../src/application/use-cases/scan-invoices.js';
import { exportInvoices } from '../../src/application/use-cases/export-invoices.js';
import { verifySunat } from '../../src/application/use-cases/verify-sunat.js';
import { ExtractionSchema } from '../../src/infrastructure/ai/extraction-schema.js';
import { writeTextPdf } from '../helpers/pdf-builder.js';
import { makeTestDeps } from '../helpers/context.js';

const opened = [];
afterEach(() => {
  for (const { db, dir } of opened.splice(0)) {
    db.close();
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

/** Factura ficticia con el formato de un proveedor digital de EE. UU. */
const FOREIGN_LINES = [
  'Invoice',
  'Invoice number IN-12345678',
  'Date of issue August 5, 2026',
  'Proveedor Digital, Inc.',
  'San Francisco, California',
  'United States',
  'US EIN 12-3456789',
  'Description Qty Unit price Amount',
  'Domain registration 1 $10.44 $10.44',
  'Subtotal $10.44',
  'Total $10.44',
  'Amount due $10.44 USD',
];

/** Proveedor de IA simulado que responde como lo hace el modelo real con este texto. */
function fakeAi() {
  const data = ExtractionSchema.parse({
    ...Object.fromEntries(Object.keys(ExtractionSchema.shape).map((k) => [k, null])),
    is_invoice: true,
    supplier_name: 'Proveedor Digital, Inc.',
    supplier_tax_id: '12-3456789',
    supplier_country: 'US',
    document_type: '91',
    number: 'IN-12345678',
    issue_date: '2026-08-05',
    currency: 'USD',
    subtotal: '10.44',
    total: '10.44',
    notes: [],
  });
  return {
    name: 'fake',
    model: 'fake-1',
    extract: vi.fn(async ({ text }) => {
      // El texto que recibe el modelo debe traer el número de factura con su guion.
      expect(text).toContain('IN-12345678');
      return { data, raw: JSON.stringify(data), model: 'fake-1', usage: {} };
    }),
  };
}

describe('cuando el modelo duda si es un comprobante', () => {
  const extraction = (overrides) =>
    ExtractionSchema.parse({
      ...Object.fromEntries(Object.keys(ExtractionSchema.shape).map((k) => [k, null])),
      notes: [],
      ...overrides,
    });
  const provider = (data) => ({
    name: 'fake',
    model: 'fake-1',
    extract: async () => ({ data, raw: JSON.stringify(data), model: 'fake-1', usage: {} }),
  });

  it('procesa para revisión un documento con datos completos de comprobante', async () => {
    // Caso real: una boleta electrónica bien leída, pero con is_invoice=false.
    const data = extraction({
      is_invoice: false,
      supplier_name: 'MUEBLES DE EJEMPLO SAC',
      supplier_ruc: '20100047218',
      document_type: '03',
      series: 'B002',
      number: '004884',
      issue_date: '2026-10-22',
      currency: 'PEN',
      taxable_amount: '355.93',
      igv: '64.07',
      total: '420.00',
    });
    const env = makeTestDeps({ aiProvider: provider(data) });
    opened.push(env);
    writeTextPdf(path.join(env.dir, 'boleta.pdf'), FOREIGN_LINES);

    const summary = await scanInvoices({ root: path.join(env.dir, 'boleta.pdf'), deps: env.deps });
    expect(summary).toMatchObject({ extracted: 1, warning: 1, failed: 0 });
    expect(env.db.prepare('SELECT rule_code FROM validation_issues').pluck().all()).toEqual([
      'MODEL_DOUBTED_INVOICE',
    ]);
  });

  it('descarta lo que no tiene datos de comprobante', async () => {
    const data = extraction({ is_invoice: false, supplier_name: 'Banco', currency: 'PEN' });
    const env = makeTestDeps({ aiProvider: provider(data) });
    opened.push(env);
    writeTextPdf(path.join(env.dir, 'estado-de-cuenta.pdf'), FOREIGN_LINES);

    const summary = await scanInvoices({
      root: path.join(env.dir, 'estado-de-cuenta.pdf'),
      deps: env.deps,
    });
    expect(summary).toMatchObject({ extracted: 0, failed: 1 });
    expect(env.db.prepare('SELECT error_kind FROM documents').pluck().get()).toBe('not_invoice');
  });
});

describe('facturas de proveedores del exterior', () => {
  it('se registran como tipo 91, se exportan con su identificador y no se consultan en SUNAT', async () => {
    const env = makeTestDeps({ aiProvider: fakeAi() });
    opened.push(env);
    const inbox = path.join(env.dir, 'in');
    fs.mkdirSync(inbox);
    writeTextPdf(path.join(inbox, 'proveedor-digital.pdf'), FOREIGN_LINES);
    // Una copia con otro contenido del mismo comprobante: posible duplicado.
    writeTextPdf(path.join(inbox, 'proveedor-digital-copia.pdf'), [...FOREIGN_LINES, 'Copy']);

    const summary = await scanInvoices({ root: inbox, deps: env.deps });
    expect(summary).toMatchObject({ extracted: 2, failed: 0, possibleDuplicates: 1 });

    const invoice = env.db.prepare('SELECT * FROM invoices ORDER BY id LIMIT 1').get();
    expect(invoice).toMatchObject({
      document_type: '91',
      supplier_ruc: null,
      supplier_tax_id: '12-3456789',
      supplier_country: 'US',
      number: 'IN-12345678',
      currency: 'USD',
      total_cents: 1044,
    });
    expect(env.db.prepare('SELECT rule_code FROM validation_issues').pluck().all()).toContain(
      'FOREIGN_SUPPLIER',
    );

    const sunat = { validateReceipt: vi.fn() };
    expect((await verifySunat({ deps: env.deps, sunatClient: sunat })).candidates).toBe(0);
    expect(sunat.validateReceipt).not.toHaveBeenCalled();

    const result = await exportInvoices({ month: '2026-08', deps: env.deps });
    const lines = fs.readFileSync(result.path, 'utf8').trim().split('\r\n');
    expect(result.rows).toBe(1); // la copia queda fuera por duplicada
    expect(lines[0].endsWith(',supplier_tax_id,supplier_country')).toBe(true);
    expect(lines[1]).toContain(',91,,IN-12345678,USD,10.44,');
    expect(lines[1].endsWith(',12-3456789,US')).toBe(true);
  });
});
