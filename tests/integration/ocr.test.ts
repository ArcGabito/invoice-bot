import fs from 'node:fs';
import path from 'node:path';
import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest';
import { scanInvoices } from '../../src/application/use-cases/scan-invoices.ts';
import { DocumentRouter } from '../../src/infrastructure/documents/document-router.ts';
import { TesseractOcr } from '../../src/infrastructure/ocr/tesseract-ocr.ts';
import { ExtractionSchema } from '../../src/infrastructure/ai/extraction-schema.ts';
import { sampleInvoiceLines } from '../helpers/pdf-builder.ts';
import { writeScannedImage, writeScannedPdf } from '../helpers/scan-builder.ts';
import { makeTempDir, makeTestDeps } from '../helpers/context.ts';
import { sqlGet, sqlAll, sqlValue, sqlColumn } from '../helpers/db.ts';

// Tesseract real (WebAssembly, sin red): una sola instancia para todo el archivo.
let ocr: TesseractOcr;
let workDir: string;
const cleanups: (() => void)[] = [];

beforeAll(async () => {
  workDir = makeTempDir('invoicebot-ocr-');
  ocr = new TesseractOcr({ languageDir: path.join(workDir, 'ocr-data') });
});
afterAll(async () => {
  await ocr.close();
  for (const fn of cleanups.splice(0)) fn();
  fs.rmSync(workDir, { recursive: true, force: true });
});

async function setup({
  withOcr = true,
  visionExtractor = null,
}: { withOcr?: boolean; visionExtractor?: any } = {}) {
  const env = await makeTestDeps();
  cleanups.push(() => {
    fs.rmSync(env.dir, { recursive: true, force: true });
  });
  env.deps.documentRouter = new DocumentRouter({
    minTextChars: env.deps.config.minTextChars,
    ocrEngine: withOcr ? ocr : null,
  });
  if (visionExtractor) env.deps.visionExtractor = visionExtractor;
  const inbox = path.join(env.dir, 'in');
  fs.mkdirSync(inbox);
  return { ...env, inbox };
}

describe('OCR local con Tesseract', { timeout: 120_000 }, () => {
  it('lee una foto y un PDF escaneado sin descargar nada ni dejar archivos en la carpeta de trabajo', async () => {
    const before = new Set(fs.readdirSync(process.cwd()));
    const photo = writeScannedImage(path.join(workDir, 'foto.jpg'), sampleInvoiceLines());
    const scan = writeScannedPdf(path.join(workDir, 'escaneo.pdf'), sampleInvoiceLines());

    for (const [filePath, mimeType] of [
      [photo, 'image/jpeg'],
      [scan, 'application/pdf'],
    ]) {
      const result = await ocr.recognize({ filePath, mimeType });
      expect(result.text).toContain('20100047218');
      expect(result.text).toContain('F001-00001234');
      expect(result.text).toMatch(/Importe Total: S\/ 118\.00/);
      expect(result.confidence).toBeGreaterThan(80);
      expect(result).toMatchObject({ pageCount: 1, pagesRead: 1 });
    }
    expect(fs.readdirSync(path.join(workDir, 'ocr-data')).sort()).toEqual([
      'eng.traineddata.gz',
      'spa.traineddata.gz',
    ]);
    const after = fs.readdirSync(process.cwd()).filter((f) => !before.has(f));
    expect(after).toEqual([]);
  });

  it('procesa comprobantes escaneados de punta a punta y deja constancia del OCR', async () => {
    const { deps, db, inbox } = await setup();
    writeScannedPdf(path.join(inbox, 'escaneada.pdf'), sampleInvoiceLines());
    writeScannedImage(path.join(inbox, 'foto.png'), sampleInvoiceLines({ number: '00000002' }));

    const summary = await scanInvoices({ root: inbox, deps });
    expect(summary).toMatchObject({ found: 2, extracted: 2, valid: 2, needsOcr: 0, failed: 0 });

    const docs = await sqlAll(
      db,
      'SELECT file_name, status, text_method FROM documents ORDER BY file_name',
    );
    expect(docs).toEqual([
      { file_name: 'escaneada.pdf', status: 'extracted', text_method: 'ocr' },
      { file_name: 'foto.png', status: 'extracted', text_method: 'ocr' },
    ]);
    const invoice = await sqlGet(db, "SELECT * FROM invoices WHERE number = '00001234'");
    expect(invoice).toMatchObject({
      supplier_ruc: '20100047218',
      total_cents: 11800,
      igv_cents: 1800,
    });
    const ocrNote = await sqlGet(
      db,
      "SELECT message, actual_value FROM validation_issues WHERE invoice_id = ? AND rule_code = 'OCR_SOURCE'",
      invoice.id,
    );
    expect(ocrNote.message).toMatch(/OCR \(tesseract\)/);
    expect(ocrNote.actual_value).toMatch(/^confianza \d+ %$/);
  });

  it('una foto torcida y con manchas queda con advertencia de baja confianza', async () => {
    const { deps, db, inbox } = await setup();
    writeScannedImage(path.join(inbox, 'foto-mala.png'), sampleInvoiceLines(), {
      rotateDeg: 1.5,
      noise: 3000,
    });
    await scanInvoices({ root: inbox, deps });
    const codes = await sqlColumn(db, 'SELECT rule_code FROM validation_issues ORDER BY id');
    expect(codes).toContain('OCR_LOW_CONFIDENCE');
    expect(await sqlValue(db, 'SELECT validation_status FROM invoices')).not.toBe('valid');
  });

  it('al activar el OCR se reprocesan los documentos que quedaron pendientes de OCR', async () => {
    const { deps, db, inbox } = await setup({ withOcr: false });
    writeScannedPdf(path.join(inbox, 'escaneada.pdf'), sampleInvoiceLines());
    expect((await scanInvoices({ root: inbox, deps })).needsOcr).toBe(1);
    expect(await sqlValue(db, 'SELECT status FROM documents')).toBe('needs_ocr');

    deps.documentRouter = new DocumentRouter({
      minTextChars: deps.config.minTextChars,
      ocrEngine: ocr,
    });
    const second = await scanInvoices({ root: inbox, deps });
    expect(second).toMatchObject({ queued: 1, extracted: 1, needsOcr: 0 });
    expect(await sqlValue(db, 'SELECT status FROM documents')).toBe('extracted');
  });

  it('si el OCR no encuentra texto, el documento sigue pendiente con una explicación', async () => {
    const { deps, db, inbox } = await setup();
    writeScannedImage(path.join(inbox, 'en-blanco.png'), []);
    expect((await scanInvoices({ root: inbox, deps })).needsOcr).toBe(1);
    expect(await sqlValue(db, 'SELECT error_message FROM documents')).toMatch(
      /OCR no encontró texto/,
    );
  });
});

describe('lectura con visión (Claude)', () => {
  it('envía el documento sin texto al lector con visión y lo registra como tal', async () => {
    const data = ExtractionSchema.parse({
      ...Object.fromEntries(Object.keys(ExtractionSchema.shape).map((k) => [k, null])),
      is_invoice: true,
      supplier_name: 'PROVEEDOR DE EJEMPLO S.A.C.',
      supplier_ruc: '20100047218',
      document_type: '01',
      series: 'F001',
      number: '00001234',
      issue_date: '2026-10-15',
      currency: 'PEN',
      taxable_amount: '100.00',
      igv: '18.00',
      total: '118.00',
      notes: [],
    });
    const vision = {
      name: 'anthropic',
      model: 'claude-opus-5-5',
      extractFromFile: vi.fn(async () => ({
        data,
        raw: '{}',
        model: 'claude-opus-5-5',
        usage: {},
      })),
    };
    const { deps, db, inbox } = await setup({ withOcr: false, visionExtractor: vision });
    const file = writeScannedImage(path.join(inbox, 'foto.jpg'), sampleInvoiceLines());

    const summary = await scanInvoices({ root: inbox, deps });
    expect(summary).toMatchObject({ extracted: 1, valid: 1 });
    expect(vision.extractFromFile).toHaveBeenCalledWith({
      filePath: file,
      mimeType: 'image/jpeg',
      fileName: 'foto.jpg',
    });
    expect(await sqlGet(db, 'SELECT provider, text_method FROM extraction_runs')).toEqual({
      provider: 'anthropic-vision',
      text_method: 'ocr',
    });
    expect(
      await sqlValue(db, "SELECT message FROM validation_issues WHERE rule_code = 'OCR_SOURCE'"),
    ).toMatch(/visión/);
  });
});
