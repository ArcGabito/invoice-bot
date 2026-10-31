import fs from 'node:fs';
import path from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { scanInvoices } from '../../src/application/use-cases/scan-invoices.ts';
import { exportInvoices } from '../../src/application/use-cases/export-invoices.ts';
import { MockProvider } from '../../src/infrastructure/ai/providers/mock-provider.ts';
import { sampleInvoiceLines, writeTextPdf } from '../helpers/pdf-builder.ts';
import { buildCdr, buildUblInvoice, writeUbl } from '../helpers/ubl-builder.ts';
import { makeTestDeps } from '../helpers/context.ts';
import type { TestContext } from '../helpers/context.ts';
import { sqlGet, sqlAll, sqlValue, sqlColumn } from '../helpers/db.ts';

const opened: TestContext[] = [];
afterEach(async () => {
  for (const { dir } of opened.splice(0)) {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

async function setup() {
  const provider = new MockProvider();
  const extractSpy = vi.spyOn(provider, 'extract');
  const env = await makeTestDeps({ aiProvider: provider });
  opened.push(env);
  const inbox = path.join(env.dir, 'facturas');
  fs.mkdirSync(inbox);
  return { ...env, inbox, extractSpy };
}

describe('comprobantes electrónicos XML', () => {
  it('lee el XML sin IA y vincula su PDF sin enviarlo al modelo', async () => {
    const { deps, db, inbox, extractSpy } = await setup();
    const stem = '20100047218-01-F001-00001234';
    writeUbl(path.join(inbox, `${stem}.xml`), buildUblInvoice());
    writeTextPdf(path.join(inbox, `${stem}.pdf`), sampleInvoiceLines());
    writeUbl(path.join(inbox, `R-${stem}.xml`), buildCdr());
    // Un PDF sin XML sí pasa por la IA.
    writeTextPdf(path.join(inbox, 'otra.pdf'), sampleInvoiceLines({ number: '00000077' }));

    const summary = await scanInvoices({ root: inbox, deps });

    expect(summary).toMatchObject({
      found: 4,
      extracted: 2,
      valid: 2,
      fromXml: 1,
      linkedToXml: 1,
      skipped: 1,
      failed: 0,
      possibleDuplicates: 0,
    });
    expect(extractSpy).toHaveBeenCalledTimes(1);

    const xmlInvoice = await sqlGet(db, "SELECT * FROM invoices WHERE data_source = 'xml'");
    expect(xmlInvoice).toMatchObject({ series: 'F001', number: '00001234', total_cents: 11800 });
    const run = await sqlGet(
      db,
      'SELECT * FROM extraction_runs WHERE id = ?',
      xmlInvoice.extraction_run_id,
    );
    expect(run).toMatchObject({ provider: 'ubl-xml', text_method: 'xml', model: null });
    expect(run.prompt_version).toMatch(/^ubl-2\.1/);

    const docs = await sqlAll(
      db,
      // Orden binario (mayúsculas antes que minúsculas), independiente de la intercalación del servidor.
      'SELECT file_name, status, skip_reason, linked_document_id FROM documents ORDER BY file_name COLLATE Latin1_General_BIN2',
    );
    expect(docs).toEqual([
      {
        file_name: `${stem}.pdf`,
        status: 'skipped',
        skip_reason: 'printed_representation',
        linked_document_id: xmlInvoice.document_id,
      },
      {
        file_name: `${stem}.xml`,
        status: 'extracted',
        skip_reason: null,
        linked_document_id: null,
      },
      {
        file_name: 'R-20100047218-01-F001-00001234.xml',
        status: 'skipped',
        skip_reason: 'cdr',
        linked_document_id: null,
      },
      { file_name: 'otra.pdf', status: 'extracted', skip_reason: null, linked_document_id: null },
    ]);

    // Idempotente: nada se vuelve a procesar ni a enviar a la IA.
    const again = await scanInvoices({ root: inbox, deps });
    expect(again).toMatchObject({ queued: 0, alreadyProcessed: 4 });
    expect(extractSpy).toHaveBeenCalledTimes(1);
  });

  it('al exportar prefiere el XML frente a un PDF del mismo comprobante leído con IA', async () => {
    const { deps, inbox } = await setup();
    // Primero llega solo el PDF (con otro nombre) y se procesa con IA.
    writeTextPdf(path.join(inbox, 'factura-escaneada.pdf'), sampleInvoiceLines());
    await scanInvoices({ root: inbox, deps });
    // Después llega el XML del mismo comprobante.
    writeUbl(path.join(inbox, 'F001-00001234.xml'), buildUblInvoice());
    const second = await scanInvoices({ root: inbox, deps });
    expect(second.possibleDuplicates).toBe(1);

    const result = await exportInvoices({ month: '2026-10', deps });
    const lines = fs.readFileSync(result.path, 'utf8').trim().split('\r\n');
    expect(result.rows).toBe(1);
    expect(lines[1]).toMatch(/,F001-00001234\.xml,[0-9a-f]{64},xml,,,,PE,pending$/);
  });

  it('marca como error un XML con importes incoherentes', async () => {
    const { deps, db, inbox } = await setup();
    writeUbl(path.join(inbox, 'mal.xml'), buildUblInvoice({ payable: '120.00' }));
    const summary = await scanInvoices({ root: inbox, deps });
    expect(summary.error).toBe(1);
    expect(await sqlColumn(db, 'SELECT rule_code FROM validation_issues ORDER BY id')).toContain(
      'TOTAL_MISMATCH',
    );
  });

  it('registra un XML mal formado como fallido no recuperable', async () => {
    const { deps, db, inbox } = await setup();
    fs.writeFileSync(path.join(inbox, 'roto.xml'), '<Invoice><cbc:ID>F001</Invoice');
    const summary = await scanInvoices({ root: inbox, deps });
    expect(summary.failed).toBe(1);
    expect(await sqlValue(db, 'SELECT error_kind FROM documents')).toBe('unreadable');
  });
});
