import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { sampleInvoiceLines, writeTextPdf } from '../helpers/pdf-builder.ts';
import { buildUblInvoice, writeUbl } from '../helpers/ubl-builder.ts';
import { acquireLock } from '../../src/infrastructure/runtime/lock.ts';
import { makeTempDir } from '../helpers/context.ts';
import { resetDatabase, workerDatabase, workerDatabaseEnv } from '../helpers/db.ts';

const BIN = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../src/cli/bin.ts');
const workDir = makeTempDir('invoicebot-e2e-run-');
afterAll(() => fs.rmSync(workDir, { recursive: true, force: true }));

// La CLI usa la base de SQL Server de este proceso de pruebas, vacía al empezar.
beforeAll(async () => {
  await resetDatabase(await workerDatabase());
});

const baseEnv = {
  ...process.env,
  ...workerDatabaseEnv(),
  INVOICEBOT_AI_PROVIDER: 'mock',
  INVOICEBOT_LOG_LEVEL: 'silent',
  INVOICEBOT_LOG_DIR: './logs',
  INVOICEBOT_RUN_EXPORT: 'xlsx',
  INVOICEBOT_SUNAT_ENABLED: 'false',
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
    const release = await acquireLock(await workerDatabase());
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
});
