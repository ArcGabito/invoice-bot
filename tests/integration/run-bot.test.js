import fs from 'node:fs';
import path from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import { currentAndPreviousMonth, runBot } from '../../src/application/use-cases/run-bot.js';
import { createBackup, isBackupDue } from '../../src/infrastructure/database/backup.js';
import { MockProvider } from '../../src/infrastructure/ai/providers/mock-provider.js';
import { ExtractionError } from '../../src/shared/errors.js';
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

function setup({ aiProvider, env } = {}) {
  const ctx = makeTestDeps({ aiProvider, env });
  opened.push(ctx);
  const inbox = path.join(ctx.dir, 'bandeja');
  fs.mkdirSync(path.join(inbox, 'proveedor-a'), { recursive: true });
  const backupDir = ctx.deps.config.backup.dir;
  const backup = {
    isDue: () => isBackupDue(backupDir, 24),
    create: () => createBackup(ctx.db, backupDir, { retention: 3 }),
  };
  return { ...ctx, inbox, backup };
}

describe('currentAndPreviousMonth', () => {
  it('incluye el cambio de año', () => {
    expect(currentAndPreviousMonth('2026-10-30')).toEqual(['2026-10', '2026-09']);
    expect(currentAndPreviousMonth('2027-01-03')).toEqual(['2027-01', '2026-12']);
  });
});

describe('runBot', () => {
  it('ejecuta un ciclo completo y lo registra en bot_runs', async () => {
    const { deps, db, inbox, backup } = setup();
    writeUbl(path.join(inbox, 'proveedor-a', 'f1.xml'), buildUblInvoice({ id: 'F001-00000001' }));
    writeTextPdf(
      path.join(inbox, 'septiembre.pdf'),
      sampleInvoiceLines({ number: '00000980', date: '28/09/2026' }),
    );

    const result = await runBot({
      deps,
      inboxDir: inbox,
      trigger: 'scheduled',
      exportFormat: 'xlsx',
      backup,
      host: 'equipo-prueba',
      pid: 1234,
    });

    expect(result).toMatchObject({ status: 'success', exitCode: 0 });
    // Recorre subcarpetas por defecto.
    expect(result.report.scan).toMatchObject({ found: 2, extracted: 2, fromXml: 1 });
    // Mes actual y anterior (hoy es 2026-10-28 en las pruebas).
    expect(result.report.exports.map((e) => [path.basename(e.path), e.rows])).toEqual([
      ['invoices-2026-10.xlsx', 1],
      ['invoices-2026-09.xlsx', 1],
    ]);
    expect(fs.existsSync(result.report.backup.path)).toBe(true);

    const [run] = db.prepare('SELECT * FROM bot_runs').all();
    expect(run).toMatchObject({
      trigger: 'scheduled',
      status: 'success',
      exit_code: 0,
      host: 'equipo-prueba',
      pid: 1234,
    });
    expect(run.finished_at).not.toBeNull();
    expect(JSON.parse(run.summary)).toMatchObject({ found: 2, extracted: 2, fromXml: 1 });

    // Segundo ciclo: nada nuevo y la copia del día ya existe.
    const second = await runBot({ deps, inboxDir: inbox, backup });
    expect(second.report.scan).toMatchObject({ alreadyProcessed: 2, extracted: 0 });
    expect(second.report.backup).toBeNull();
    expect(second.report.exports).toEqual([]);
  });

  it('reintenta en el siguiente ciclo lo que falló por un error temporal', async () => {
    const provider = new MockProvider();
    const real = provider.extract.bind(provider);
    let online = false;
    provider.extract = async (input) => {
      if (!online)
        throw new ExtractionError('sin conexión', { kind: 'transient', retryable: true });
      return real(input);
    };
    const { deps, inbox } = setup({ aiProvider: provider });
    writeTextPdf(path.join(inbox, 'f.pdf'), sampleInvoiceLines());

    const first = await runBot({ deps, inboxDir: inbox });
    expect(first).toMatchObject({ status: 'partial', exitCode: 3 });
    expect(first.report.scan.failed).toBe(1);

    online = true;
    const second = await runBot({ deps, inboxDir: inbox });
    expect(second).toMatchObject({ status: 'success', exitCode: 0 });
    expect(second.report.retry).toMatchObject({ queued: 1, extracted: 1 });
  });

  it('una verificación de SUNAT fallida deja el ciclo como parcial, no lo detiene', async () => {
    const { deps, inbox, backup } = setup();
    writeUbl(path.join(inbox, 'f1.xml'), buildUblInvoice());
    const sunatClient = {
      validateReceipt: async () => {
        throw Object.assign(new Error('Servicio de SUNAT no disponible'), { kind: 'transient' });
      },
    };
    const result = await runBot({ deps, inboxDir: inbox, sunatClient, backup });
    expect(result).toMatchObject({ status: 'partial', exitCode: 3 });
    expect(result.report.sunat).toMatchObject({ failed: 1 });
    expect(result.report.backup).not.toBeNull();
  });

  it('registra como fallido un ciclo que no puede ni empezar', async () => {
    const { deps, db } = setup();
    const result = await runBot({ deps, inboxDir: path.join(deps.config.dataDir, 'no-existe') });
    expect(result).toMatchObject({ status: 'failed', exitCode: 1 });
    expect(result.error).toMatch(/No existe la ruta/);
    expect(db.prepare('SELECT status, error_message FROM bot_runs').get()).toMatchObject({
      status: 'failed',
      error_message: expect.stringMatching(/No existe la ruta/),
    });
  });

  it('cierra como fallidas las ejecuciones que quedaron abiertas por un cierre inesperado', () => {
    const { deps } = setup();
    deps.repository.startBotRun({ trigger: 'scheduled', host: 'x', pid: 1 });
    expect(deps.repository.closeAbandonedBotRuns()).toBe(1);
    expect(deps.repository.listBotRuns()[0]).toMatchObject({
      status: 'failed',
      error_message: 'La ejecución terminó de forma inesperada',
    });
  });
});
