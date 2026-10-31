import fs from 'node:fs';
import path from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { scanInvoices } from '../../src/application/use-cases/scan-invoices.js';
import { retryDocuments } from '../../src/application/use-cases/retry-failed.js';
import { exportInvoices } from '../../src/application/use-cases/export-invoices.js';
import { findDuplicates } from '../../src/application/use-cases/find-duplicates.js';
import { MockProvider } from '../../src/infrastructure/ai/providers/mock-provider.js';
import { ExtractionError } from '../../src/shared/errors.js';
import { sampleInvoiceLines, writeTextPdf } from '../helpers/pdf-builder.js';
import { makeTestDeps } from '../helpers/context.js';

const opened = [];
afterEach(() => {
  for (const { db, dir } of opened.splice(0)) {
    db.close();
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

function setup(options) {
  const env = makeTestDeps(options);
  opened.push(env);
  const inbox = path.join(env.dir, 'facturas');
  fs.mkdirSync(inbox);
  return { ...env, inbox };
}

const count = (db, table) => db.prepare(`SELECT COUNT(*) FROM ${table}`).pluck().get();

describe('scanInvoices', () => {
  it('procesa un lote mixto sin que un fallo detenga el resto', async () => {
    const { deps, db, inbox } = setup();
    writeTextPdf(path.join(inbox, 'a-valida.pdf'), sampleInvoiceLines());
    fs.copyFileSync(path.join(inbox, 'a-valida.pdf'), path.join(inbox, 'b-copia.pdf'));
    // Mismo comprobante con otro contenido (p. ej. reimpresión): posible duplicado.
    writeTextPdf(path.join(inbox, 'c-reimpresion.pdf'), [
      ...sampleInvoiceLines(),
      'Representación impresa',
    ]);
    writeTextPdf(
      path.join(inbox, 'd-error.pdf'),
      sampleInvoiceLines({ number: '00000999', total: '200.00' }),
    );
    writeTextPdf(path.join(inbox, 'e-escaneada.pdf'), []);
    fs.writeFileSync(path.join(inbox, 'f-foto.jpg'), Buffer.from([0xff, 0xd8, 0xff]));
    fs.writeFileSync(path.join(inbox, 'g-danada.pdf'), 'esto no es un pdf');
    fs.writeFileSync(path.join(inbox, 'notas.txt'), 'no compatible');

    const summary = await scanInvoices({ root: inbox, deps });

    expect(summary).toMatchObject({
      found: 7,
      extracted: 3,
      valid: 1,
      warning: 1,
      error: 1,
      needsOcr: 2,
      failed: 1,
      exactDuplicates: 1,
      possibleDuplicates: 1,
    });
    expect(summary.unsupported).toEqual([path.join(inbox, 'notas.txt')]);
    expect(summary.failures[0].file).toContain('g-danada.pdf');

    // Todos los archivos compatibles quedan registrados, también los fallidos.
    expect(count(db, 'documents')).toBe(6);
    expect(count(db, 'invoices')).toBe(3);
    const failed = db.prepare("SELECT * FROM documents WHERE status = 'failed'").get();
    expect(failed.error_kind).toBe('unreadable');

    const invoice = db.prepare("SELECT * FROM invoices WHERE validation_status = 'valid'").get();
    expect(invoice).toMatchObject({
      supplier_ruc: '20100047218',
      series: 'F001',
      total_cents: 11800,
      igv_cents: 1800,
      issue_date: '2026-10-15',
    });

    // Trazabilidad: proveedor, modelo y versión de prompt de cada extracción.
    const run = db
      .prepare('SELECT * FROM extraction_runs WHERE id = ?')
      .get(invoice.extraction_run_id);
    expect(run).toMatchObject({
      provider: 'mock',
      model: 'regex-heuristics-v1',
      status: 'success',
      text_method: 'pdf_text',
    });
    expect(run.prompt_version).toMatch(/^invoice-extraction\//);

    const dupIssue = db
      .prepare("SELECT * FROM validation_issues WHERE rule_code = 'POSSIBLE_DUPLICATE'")
      .get();
    // Con concurrencia, el que termina segundo es el que recibe la advertencia.
    expect(dupIssue.actual_value).toMatch(/a-valida\.pdf|c-reimpresion\.pdf/);

    const { exact, possible } = findDuplicates({ deps });
    expect(exact).toHaveLength(1);
    expect(exact[0].items.map((i) => path.basename(i.file_path))).toEqual([
      'a-valida.pdf',
      'b-copia.pdf',
    ]);
    expect(possible).toHaveLength(1);
    expect(possible[0].items).toHaveLength(2);
  });

  it('es idempotente: una segunda ejecución no vuelve a llamar a la IA', async () => {
    const provider = new MockProvider();
    const spy = vi.spyOn(provider, 'extract');
    const { deps, db, inbox } = setup({ aiProvider: provider });
    writeTextPdf(path.join(inbox, 'f1.pdf'), sampleInvoiceLines());
    writeTextPdf(path.join(inbox, 'f2.pdf'), sampleInvoiceLines({ number: '00000002' }));

    await scanInvoices({ root: inbox, deps });
    expect(spy).toHaveBeenCalledTimes(2);

    const second = await scanInvoices({ root: inbox, deps });
    expect(spy).toHaveBeenCalledTimes(2);
    expect(second).toMatchObject({ found: 2, alreadyProcessed: 2, queued: 0, extracted: 0 });
    expect(count(db, 'invoices')).toBe(2);
    expect(count(db, 'file_occurrences')).toBe(2);
  });

  it('el modo simulación no escribe en la base de datos ni llama a la IA', async () => {
    const provider = new MockProvider();
    const spy = vi.spyOn(provider, 'extract');
    const { deps, db, inbox } = setup({ aiProvider: provider });
    writeTextPdf(path.join(inbox, 'f1.pdf'), sampleInvoiceLines());
    fs.copyFileSync(path.join(inbox, 'f1.pdf'), path.join(inbox, 'f1-copia.pdf'));

    const summary = await scanInvoices({ root: inbox, deps, dryRun: true });
    expect(summary).toMatchObject({ found: 2, queued: 1, exactDuplicates: 1 });
    expect(spy).not.toHaveBeenCalled();
    expect(count(db, 'documents')).toBe(0);
  });

  it('recorre subcarpetas solo con --recursive', async () => {
    const { deps, inbox } = setup();
    fs.mkdirSync(path.join(inbox, '2026', '10'), { recursive: true });
    writeTextPdf(path.join(inbox, '2026', '10', 'f1.pdf'), sampleInvoiceLines());
    expect((await scanInvoices({ root: inbox, deps, dryRun: true })).found).toBe(0);
    expect((await scanInvoices({ root: inbox, deps, recursive: true, dryRun: true })).found).toBe(
      1,
    );
  });

  it('reanuda documentos que quedaron en proceso tras una interrupción', async () => {
    const { deps, db, inbox } = setup();
    writeTextPdf(path.join(inbox, 'f1.pdf'), sampleInvoiceLines());
    await scanInvoices({ root: inbox, deps, dryRun: false });
    // Simula una interrupción a mitad de la extracción.
    db.prepare("UPDATE documents SET status = 'processing'").run();
    db.prepare('DELETE FROM invoices').run();

    const summary = await scanInvoices({ root: inbox, deps });
    expect(summary).toMatchObject({ queued: 1, extracted: 1, valid: 1 });
    expect(count(db, 'invoices')).toBe(1);
  });

  it('repite una vez la extracción si el modelo devuelve una salida inválida', async () => {
    const provider = new MockProvider();
    const real = provider.extract.bind(provider);
    let calls = 0;
    provider.extract = async (input) => {
      if (calls++ === 0)
        throw new ExtractionError('JSON inválido', {
          kind: 'invalid_output',
          retryable: true,
          rawResponse: '{',
        });
      return real(input);
    };
    const { deps, db, inbox } = setup({ aiProvider: provider });
    writeTextPdf(path.join(inbox, 'f1.pdf'), sampleInvoiceLines());

    const summary = await scanInvoices({ root: inbox, deps });
    expect(summary.extracted).toBe(1);
    const runs = db
      .prepare('SELECT status, error_kind, raw_response FROM extraction_runs ORDER BY id')
      .all();
    expect(runs).toEqual([
      { status: 'failed', error_kind: 'invalid_output', raw_response: '{' },
      expect.objectContaining({ status: 'success' }),
    ]);
  });
});

describe('retryDocuments', () => {
  it('reintenta fallos transitorios sin duplicar la factura', async () => {
    const provider = new MockProvider();
    const real = provider.extract.bind(provider);
    let online = false;
    provider.extract = async (input) => {
      if (!online)
        throw new ExtractionError('sin conexión', { kind: 'transient', retryable: true });
      return real(input);
    };
    const { deps, db, inbox } = setup({ aiProvider: provider });
    writeTextPdf(path.join(inbox, 'f1.pdf'), sampleInvoiceLines());

    const first = await scanInvoices({ root: inbox, deps });
    expect(first.failed).toBe(1);
    expect(db.prepare('SELECT status, attempts, error_kind FROM documents').get()).toEqual({
      status: 'failed',
      attempts: 1,
      error_kind: 'transient',
    });

    // Un escaneo normal no reintenta documentos fallidos.
    expect((await scanInvoices({ root: inbox, deps })).previouslyFailed).toBe(1);

    online = true;
    const retry = await retryDocuments({ deps });
    expect(retry).toMatchObject({ queued: 1, extracted: 1, valid: 1 });
    expect(db.prepare('SELECT status, attempts FROM documents').get()).toEqual({
      status: 'extracted',
      attempts: 2,
    });

    await retryDocuments({ deps, force: true });
    expect(count(db, 'invoices')).toBe(1);
  });

  it('no reintenta errores permanentes ni documentos que agotaron sus intentos', async () => {
    const provider = new MockProvider();
    provider.extract = async () => {
      throw new ExtractionError('rechazado', { kind: 'refused', retryable: false });
    };
    const { deps, inbox } = setup({ aiProvider: provider });
    writeTextPdf(path.join(inbox, 'f1.pdf'), sampleInvoiceLines());
    await scanInvoices({ root: inbox, deps });

    const retry = await retryDocuments({ deps });
    expect(retry.queued).toBe(0);
    expect(retry.skipped[0].reason).toContain('refused');
  });

  it('marca el documento si el archivo original ya no existe', async () => {
    const provider = new MockProvider();
    provider.extract = async () => {
      throw new ExtractionError('sin conexión', { kind: 'transient', retryable: true });
    };
    const { deps, db, inbox } = setup({ aiProvider: provider });
    const file = writeTextPdf(path.join(inbox, 'f1.pdf'), sampleInvoiceLines());
    await scanInvoices({ root: inbox, deps });
    fs.rmSync(file);

    const retry = await retryDocuments({ deps });
    expect(retry.failed).toBe(1);
    expect(db.prepare('SELECT error_kind FROM documents').pluck().get()).toBe('file_missing');
  });
});

describe('exportInvoices', () => {
  it('exporta el mes con encabezados estables, sin errores ni duplicados por defecto', async () => {
    // Concurrencia 1: así es fijo qué copia recibe la advertencia de duplicado.
    const { deps, inbox } = setup({ env: { INVOICEBOT_CONCURRENCY: '1' } });
    writeTextPdf(path.join(inbox, 'a.pdf'), sampleInvoiceLines());
    writeTextPdf(path.join(inbox, 'b-reimpresion.pdf'), [...sampleInvoiceLines(), 'copia']);
    writeTextPdf(
      path.join(inbox, 'c-error.pdf'),
      sampleInvoiceLines({ number: '00000999', total: '999.00' }),
    );
    writeTextPdf(
      path.join(inbox, 'd-septiembre.pdf'),
      sampleInvoiceLines({ number: '00000100', date: '30/09/2026' }),
    );
    await scanInvoices({ root: inbox, deps });

    const result = exportInvoices({ month: '2026-10', deps });
    const lines = fs.readFileSync(result.path, 'utf8').trim().split('\r\n');
    expect(result.rows).toBe(1);
    expect(lines[0]).toBe(
      'invoice_id,issue_date,due_date,supplier_ruc,supplier_name,customer_ruc,customer_name,document_type,series,number,currency,' +
        'subtotal,taxable_amount,exempt_amount,unaffected_amount,discount_amount,igv,isc,icbper,other_charges,other_taxes,total,' +
        'validation_status,duplicate_count,file_name,sha256,data_source,sunat_status',
    );
    expect(lines[1]).toContain(',2026-10-15,,20100047218,PROVEEDOR DE EJEMPLO S.A.C.,20131312955,');
    expect(lines[1]).toContain(',01,F001,00001234,PEN,,100.00,,,,18.00,,,,,118.00,valid,1,a.pdf,');

    expect(
      exportInvoices({ month: '2026-10', includeErrors: true, includeDuplicates: true, deps }).rows,
    ).toBe(3);
    expect(exportInvoices({ month: '2026-09', deps }).rows).toBe(1);
    expect(exportInvoices({ supplierRuc: '20999999999', deps }).rows).toBe(0);
    expect(() => exportInvoices({ month: '10-2026', deps })).toThrow(/AAAA-MM/);
  });
});
