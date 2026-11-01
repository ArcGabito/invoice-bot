import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { sampleInvoiceLines, writeTextPdf } from '../helpers/pdf-builder.ts';
import { buildUblInvoice, writeUbl } from '../helpers/ubl-builder.ts';
import { createEmptyDatabase, dropDatabase } from '../../src/infrastructure/database/admin.ts';
import { createDatabase, runMigrations } from '../../src/infrastructure/database/connection.ts';
import type { Db } from '../../src/infrastructure/database/connection.ts';
import { acquireLock } from '../../src/infrastructure/runtime/lock.ts';
import { makeTempDir } from '../helpers/context.ts';
import { requireServer, workerDatabaseEnv, workerDatabaseName } from '../helpers/db.ts';

const BIN = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../src/cli/bin.ts');
const workDir = makeTempDir('invoicebot-e2e-run-');

// Base propia, recién creada: `run` decide si toca una copia según el
// historial de copias de la base, y las de otras pruebas del mismo proceso
// (hechas sobre la base compartida) no deben contar.
const DB_NAME = `${workerDatabaseName()}_run`;
let db: Db;
beforeAll(async () => {
  const server = requireServer();
  await createEmptyDatabase(server, DB_NAME, { replace: true });
  db = createDatabase({ ...server, database: DB_NAME });
  await runMigrations(db);
});
afterAll(async () => {
  await db?.destroy();
  await dropDatabase(requireServer(), DB_NAME);
  fs.rmSync(workDir, { recursive: true, force: true });
});

const baseEnv = {
  ...process.env,
  ...workerDatabaseEnv(),
  INVOICEBOT_DB_NAME: DB_NAME,
  INVOICEBOT_AI_PROVIDER: 'mock',
  INVOICEBOT_LOG_LEVEL: 'silent',
  INVOICEBOT_LOG_DIR: './logs',
  INVOICEBOT_RUN_EXPORT: 'xlsx',
  INVOICEBOT_SUNAT_ENABLED: 'false',
  // Sin red en las pruebas: el tipo de cambio se carga con fx set.
  INVOICEBOT_FX_SUNAT: 'false',
};

function cli(env: Record<string, string>, ...args: string[]) {
  const result = spawnSync(process.execPath, [BIN, ...args], {
    cwd: workDir,
    encoding: 'utf8',
    env: { ...baseEnv, ...env },
  });
  return { code: result.status, stdout: result.stdout, stderr: result.stderr };
}

describe('ejecución desatendida desde la CLI', { timeout: 60_000 }, () => {
  fs.mkdirSync(path.join(workDir, 'bandeja'));
  writeUbl(path.join(workDir, 'bandeja', 'f1.xml'), buildUblInvoice({ issueDate: '2026-10-15' }));
  writeTextPdf(path.join(workDir, 'bandeja', 'f2.pdf'), sampleInvoiceLines({ number: '00000002' }));

  it('exige una carpeta de entrada', async () => {
    const { code, stderr } = cli({}, 'run');
    expect(code).toBe(2);
    expect(stderr).toContain('INVOICEBOT_INBOX_DIR');
  });

  it('procesa, exporta, respalda y deja registro', async () => {
    const { code, stdout } = cli(
      { INVOICEBOT_INBOX_DIR: './bandeja' },
      'run',
      '--trigger',
      'scheduled',
    );
    expect(code).toBe(0);
    expect(stdout).toMatch(/Ejecución #1 correcta/);
    expect(stdout).toContain('2 encontrados · 2 nuevos');
    expect(stdout).toMatch(/Copia de seguridad: .*invoicebot_test_\w+-\d{8}-\d{6}\.bak/);

    expect(fs.readdirSync(path.join(workDir, 'exports')).some((f) => f.endsWith('.xlsx'))).toBe(
      true,
    );
    const [logFile] = fs.readdirSync(path.join(workDir, 'logs'));
    const log = fs.readFileSync(path.join(workDir, 'logs', logFile), 'utf8');
    expect(log).toContain('inicio de ejecución');
    expect(log).toContain('fin de ejecución');

    const status = cli({}, 'status');
    expect(status.stdout).toMatch(
      /#1 .* · scheduled · success · 2 documentos, 2 nuevos, 0 fallidos/,
    );
  });

  it('omite el ciclo sin error si ya hay otra ejecución en curso', async () => {
    // Otra instancia (aquí, este proceso) tiene el candado en SQL Server.
    const release = await acquireLock(db);
    try {
      const { code, stdout } = cli({ INVOICEBOT_INBOX_DIR: './bandeja' }, 'run');
      expect(code).toBe(0);
      expect(stdout).toContain('Se omite este ciclo');
    } finally {
      await release();
    }
  });

  it('backup crea copias y las lista; export --xlsx genera Excel', async () => {
    expect(cli({}, 'backup').stdout).toContain('Copia creada y verificada');
    const list = cli({}, 'backup', '--list');
    expect(list.stdout).toMatch(/Copias registradas: [1-9]\d* \(se conservan 14\)/);
    expect(list.stdout).toMatch(/invoicebot_test_\w+-\d{8}-\d{6}\.bak/);

    const exp = cli({}, 'export', '--month', '2026-10', '--xlsx', '-o', 'reporte.xlsx');
    expect(exp.code).toBe(0);
    expect(fs.existsSync(path.join(workDir, 'reporte.xlsx'))).toBe(true);
    expect(cli({}, 'export', '--format', 'pdf').code).toBe(2);
  });

  it('fx set, import y list cargan y muestran tipos de cambio', async () => {
    const set = cli({}, 'fx', 'set', '15/10/2026', '3,750', '--compra', '3.741');
    expect(set.code).toBe(0);
    expect(set.stdout).toContain('Tipos de cambio guardados: 1 de 1');

    fs.writeFileSync(path.join(workDir, 'tc.csv'), 'fecha;compra;venta\n16/10/2026;3.744;3.752\n');
    expect(cli({}, 'fx', 'import', 'tc.csv').stdout).toContain('Tipos de cambio guardados: 1 de 1');

    const list = cli({}, 'fx', 'list', '--month', '2026-10');
    expect(list.stdout).toContain('2026-10-15  3.741  3.750  manual');
    expect(list.stdout).toContain('2026-10-16  3.744  3.752  csv');

    expect(cli({}, 'fx', 'set', '31/02/2026', '3.7').code).toBe(2);
    expect(cli({}, 'fx', 'set', '15/10/2026', '3.7', '--moneda', 'PEN').code).toBe(2);
    fs.writeFileSync(path.join(workDir, 'malo.csv'), '16/10/2026;x\n');
    const bad = cli({}, 'fx', 'import', 'malo.csv');
    expect(bad.code).toBe(2);
    expect(bad.stderr).toContain('Línea 1: importes no válidos');
  });
});
