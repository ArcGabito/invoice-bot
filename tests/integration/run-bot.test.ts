import fs from 'node:fs';
import path from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import { currentAndPreviousMonth, runBot } from '../../src/application/use-cases/run-bot.ts';
import { createBackup, listBackups } from '../../src/infrastructure/database/backup.ts';
import type { AiProvider } from '../../src/application/ports/ai-provider.ts';
import { MockProvider } from '../../src/infrastructure/ai/providers/mock-provider.ts';
import { ExtractionError } from '../../src/shared/errors.ts';
import { sampleInvoiceLines, writeTextPdf } from '../helpers/pdf-builder.ts';
import { buildUblInvoice, writeUbl } from '../helpers/ubl-builder.ts';
import { makeTestDeps } from '../helpers/context.ts';
import type { TestContext } from '../helpers/context.ts';
import { sqlGet, sqlAll } from '../helpers/db.ts';

const opened: TestContext[] = [];
afterEach(async () => {
  for (const { dir } of opened.splice(0)) {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

async function setup({
  aiProvider,
  env,
}: { aiProvider?: AiProvider; env?: Record<string, string> } = {}) {
  const ctx = await makeTestDeps({ aiProvider, env });
  opened.push(ctx);
  const inbox = path.join(ctx.dir, 'bandeja');
  fs.mkdirSync(path.join(inbox, 'proveedor-a'), { recursive: true });
  // La base de pruebas se reutiliza entre archivos: la copia "del día" es la
  // hecha durante esta prueba (isBackupDue se prueba en runtime.test.ts).
  // msdb guarda la hora al segundo.
  const since = new Date(Math.floor(Date.now() / 1000) * 1000);
  const backup = {
    isDue: async () => !(await listBackups(ctx.db)).some((b) => b.createdAt >= since),
    create: () => createBackup(ctx.db, ctx.deps.config.backup.dir, { retention: 3 }),
  };
  return { ...ctx, inbox, backup };
}

describe('currentAndPreviousMonth', () => {
  it('incluye el cambio de año', async () => {
    expect(currentAndPreviousMonth('2026-10-30')).toEqual(['2026-10', '2026-09']);
    expect(currentAndPreviousMonth('2027-01-03')).toEqual(['2027-01', '2026-12']);
  });
});

describe('runBot', () => {
  it('ejecuta un ciclo completo y lo registra en bot_runs', async () => {
    const { deps, db, inbox, backup } = await setup();
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
    // SQL Server escribe la copia en su propio disco: se comprueba en su historial.
    expect((await listBackups(db)).map((b) => b.path)).toContain(result.report.backup!.path);

    const [run] = await sqlAll(db, 'SELECT * FROM bot_runs');
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
    const { deps, inbox } = await setup({ aiProvider: provider });
    writeTextPdf(path.join(inbox, 'f.pdf'), sampleInvoiceLines());

    const first = await runBot({ deps, inboxDir: inbox });
    expect(first).toMatchObject({ status: 'partial', exitCode: 3 });
    expect(first.report.scan!.failed).toBe(1);

    online = true;
    const second = await runBot({ deps, inboxDir: inbox });
    expect(second).toMatchObject({ status: 'success', exitCode: 0 });
    expect(second.report.retry).toMatchObject({ queued: 1, extracted: 1 });
  });

  it('una verificación de SUNAT fallida deja el ciclo como parcial, no lo detiene', async () => {
    const { deps, inbox, backup } = await setup();
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
    const { deps, db } = await setup();
    const result = await runBot({ deps, inboxDir: path.join(deps.config.dataDir, 'no-existe') });
    expect(result).toMatchObject({ status: 'failed', exitCode: 1 });
    expect(result.error).toMatch(/No existe la ruta/);
    expect(await sqlGet(db, 'SELECT status, error_message FROM bot_runs')).toMatchObject({
      status: 'failed',
      error_message: expect.stringMatching(/No existe la ruta/),
    });
  });

  it('cierra como fallidas las ejecuciones que quedaron abiertas por un cierre inesperado', async () => {
    const { deps } = await setup();
    await deps.repository.startBotRun({ trigger: 'scheduled', host: 'x', pid: 1 });
    expect(await deps.repository.closeAbandonedBotRuns()).toBe(1);
    expect((await deps.repository.listBotRuns())[0]).toMatchObject({
      status: 'failed',
      error_message: 'La ejecución terminó de forma inesperada',
    });
  });
});
