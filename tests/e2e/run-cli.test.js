import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterAll, describe, expect, it } from 'vitest';
import { sampleInvoiceLines, writeTextPdf } from '../helpers/pdf-builder.js';
import { buildUblInvoice, writeUbl } from '../helpers/ubl-builder.js';
import { makeTempDir } from '../helpers/context.js';

const BIN = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../bin/invoicebot.js');
const workDir = makeTempDir('invoicebot-e2e-run-');
afterAll(() => fs.rmSync(workDir, { recursive: true, force: true }));

const baseEnv = {
  ...process.env,
  INVOICEBOT_AI_PROVIDER: 'mock',
  INVOICEBOT_LOG_LEVEL: 'silent',
  INVOICEBOT_LOG_DIR: './logs',
  INVOICEBOT_RUN_EXPORT: 'xlsx',
  INVOICEBOT_SUNAT_ENABLED: 'false',
};

function cli(env, ...args) {
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

  it('exige una carpeta de entrada', () => {
    const { code, stderr } = cli({}, 'run');
    expect(code).toBe(2);
    expect(stderr).toContain('INVOICEBOT_INBOX_DIR');
  });

  it('procesa, exporta, respalda y deja registro', () => {
    const { code, stdout } = cli(
      { INVOICEBOT_INBOX_DIR: './bandeja' },
      'run',
      '--trigger',
      'scheduled',
    );
    expect(code).toBe(0);
    expect(stdout).toMatch(/Ejecución #1 correcta/);
    expect(stdout).toContain('2 encontrados · 2 nuevos');
    expect(stdout).toMatch(/Copia de seguridad: .*invoicebot-\d{8}-\d{6}\.db/);

    expect(fs.readdirSync(path.join(workDir, 'exports')).some((f) => f.endsWith('.xlsx'))).toBe(
      true,
    );
    const [logFile] = fs.readdirSync(path.join(workDir, 'logs'));
    const log = fs.readFileSync(path.join(workDir, 'logs', logFile), 'utf8');
    expect(log).toContain('inicio de ejecución');
    expect(log).toContain('fin de ejecución');
    expect(fs.existsSync(path.join(workDir, 'data', 'invoicebot.lock'))).toBe(false);

    const status = cli({}, 'status');
    expect(status.stdout).toMatch(
      /#1 .* · scheduled · success · 2 documentos, 2 nuevos, 0 fallidos/,
    );
  });

  it('omite el ciclo sin error si ya hay otra ejecución en curso', () => {
    const lockPath = path.join(workDir, 'data', 'invoicebot.lock');
    fs.writeFileSync(
      lockPath,
      JSON.stringify({
        pid: process.pid,
        host: os.hostname(),
        startedAt: new Date().toISOString(),
      }),
    );
    try {
      const { code, stdout } = cli({ INVOICEBOT_INBOX_DIR: './bandeja' }, 'run');
      expect(code).toBe(0);
      expect(stdout).toContain('Se omite este ciclo');
      expect(fs.existsSync(lockPath)).toBe(true); // no toca el candado ajeno
    } finally {
      fs.rmSync(lockPath, { force: true });
    }
  });

  it('backup crea copias y las lista; export --xlsx genera Excel', () => {
    expect(cli({}, 'backup').stdout).toContain('Copia creada y verificada');
    const list = cli({}, 'backup', '--list');
    expect(list.stdout).toMatch(/Copias en .*: 2 \(se conservan 14\)/);

    const exp = cli({}, 'export', '--month', '2026-10', '--xlsx', '-o', 'reporte.xlsx');
    expect(exp.code).toBe(0);
    expect(fs.existsSync(path.join(workDir, 'reporte.xlsx'))).toBe(true);
    expect(cli({}, 'export', '--format', 'pdf').code).toBe(2);
  });
});
