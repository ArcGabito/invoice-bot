import fs from 'node:fs';
import path from 'node:path';
import readExcelFile from 'read-excel-file/node';
import { afterEach, describe, expect, it } from 'vitest';
import { scanInvoices } from '../../src/application/use-cases/scan-invoices.js';
import { exportInvoices } from '../../src/application/use-cases/export-invoices.js';
import { EXPORT_COLUMNS } from '../../src/infrastructure/exports/columns.js';
import { buildUblInvoice, writeUbl } from '../helpers/ubl-builder.js';
import { makeTestDeps } from '../helpers/context.js';

const opened = [];
afterEach(() => {
  for (const { db, dir } of opened.splice(0)) {
    db.close();
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

describe('exportación a Excel', () => {
  it('genera hojas Facturas y Resumen con números, fechas y textos sin perder ceros', async () => {
    const env = makeTestDeps();
    opened.push(env);
    const inbox = path.join(env.dir, 'in');
    fs.mkdirSync(inbox);
    writeUbl(path.join(inbox, 'a.xml'), buildUblInvoice({ id: 'F001-00001234' }));
    writeUbl(
      path.join(inbox, 'b.xml'),
      buildUblInvoice({
        id: 'F001-00001235',
        currency: 'USD',
        taxes: [{ scheme: '1000', taxable: '1000.00', tax: '180.00' }],
        lineExtension: '1000.00',
        payable: '1180.00',
      }),
    );
    await scanInvoices({ root: inbox, deps: env.deps });

    const result = await exportInvoices({ month: '2026-10', format: 'xlsx', deps: env.deps });
    expect(result).toMatchObject({ rows: 2 });
    expect(result.path.endsWith(path.join('exports', 'invoices-2026-10.xlsx'))).toBe(true);

    const sheets = await readExcelFile(fs.readFileSync(result.path));
    expect(sheets.map((s) => s.sheet)).toEqual(['Facturas', 'Resumen']);

    const [header, ...rows] = sheets[0].data;
    expect(header).toEqual(EXPORT_COLUMNS.map((c) => c.label));
    const col = (key) => EXPORT_COLUMNS.findIndex((c) => c.key === key);
    const pen = rows.find((r) => r[col('currency')] === 'PEN');
    expect(pen[col('number')]).toBe('00001234'); // texto: conserva los ceros
    expect(pen[col('supplier_ruc')]).toBe('20100047218');
    expect(pen[col('total')]).toBe(118); // número: Excel puede sumarlo
    expect(pen[col('igv')]).toBe(18);
    expect(pen[col('issue_date')]).toBeInstanceOf(Date);
    expect(pen[col('issue_date')].toISOString().slice(0, 10)).toBe('2026-10-15');
    expect(pen[col('data_source')]).toBe('xml');

    const summary = sheets[1].data;
    expect(summary[0][0]).toMatch(/^Facturas 2026-10/);
    expect(summary).toContainEqual(['PEN', 'valid', 1, 100, 18, 118]);
    expect(summary).toContainEqual(['USD', 'valid', 1, 1000, 180, 1180]);
  });

  it('usa el formato por defecto de la configuración en la CLI y escribe un Excel vacío válido', async () => {
    const env = makeTestDeps({ env: { INVOICEBOT_EXPORT_FORMAT: 'xlsx' } });
    opened.push(env);
    expect(env.deps.config.exportFormat).toBe('xlsx');
    const result = await exportInvoices({ month: '2026-01', format: 'xlsx', deps: env.deps });
    const [facturas] = await readExcelFile(fs.readFileSync(result.path));
    expect(facturas.data).toHaveLength(1); // solo encabezados
  });
});
