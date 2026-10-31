import { execFileSync, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterAll, describe, expect, it } from 'vitest';
import { sampleInvoiceLines, writeTextPdf } from '../helpers/pdf-builder.js';
import { makeTempDir } from '../helpers/context.js';

const BIN = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../bin/invoicebot.js');
const workDir = makeTempDir('invoicebot-e2e-');
const inbox = path.join(workDir, 'facturas');

afterAll(() => fs.rmSync(workDir, { recursive: true, force: true }));

function cli(...args) {
  const result = spawnSync(process.execPath, [BIN, ...args], {
    cwd: workDir,
    encoding: 'utf8',
    env: {
      ...process.env,
      INVOICEBOT_AI_PROVIDER: 'mock',
      INVOICEBOT_LOG_LEVEL: 'silent',
      ANTHROPIC_API_KEY: '',
    },
  });
  return { code: result.status, stdout: result.stdout, stderr: result.stderr };
}

// Cada prueba lanza procesos de Node completos; con la suite en paralelo
// pueden superar el límite por defecto de 5 s.
describe('invoicebot CLI', { timeout: 30_000 }, () => {
  fs.mkdirSync(inbox, { recursive: true });
  writeTextPdf(path.join(inbox, 'f1.pdf'), sampleInvoiceLines());
  writeTextPdf(
    path.join(inbox, 'f2.pdf'),
    sampleInvoiceLines({ number: '00000002', total: '500.00' }),
  );

  it('muestra la versión', () => {
    const pkg = JSON.parse(fs.readFileSync(new URL('../../package.json', import.meta.url), 'utf8'));
    expect(execFileSync(process.execPath, [BIN, '--version'], { encoding: 'utf8' }).trim()).toBe(
      pkg.version,
    );
  });

  it('devuelve 2 ante argumentos incorrectos', () => {
    expect(cli('scan').code).toBe(2);
    expect(cli('export', '--month', '2026-1').code).toBe(2);
    expect(cli('--provider', 'chatbot', 'config').code).toBe(2);
    // La verificación en SUNAT está desactivada por defecto y lo explica.
    const sunat = cli('verify-sunat');
    expect(sunat.code).toBe(2);
    expect(sunat.stderr).toContain('INVOICEBOT_SUNAT_ENABLED=true');
    expect(cli('scan', './facturas', '--verify-sunat').code).toBe(2);
  });

  it('scan devuelve 3 si hay facturas con errores y muestra el resumen', () => {
    const { code, stdout } = cli('scan', './facturas');
    expect(code).toBe(3);
    expect(stdout).toContain('Documentos encontrados: 2');
    expect(stdout).toContain('Procesados correctamente: 1');
    expect(stdout).toContain('Con errores: 1');
    expect(stdout).toContain(path.join(workDir, 'data', 'invoicebot.db'));
  });

  it('scan repetido omite lo ya procesado y termina con 0', () => {
    const { code, stdout } = cli('scan', './facturas');
    expect(code).toBe(0);
    expect(stdout).toContain('Ya procesados (omitidos): 2');
  });

  it('status y review ofrecen salida JSON', () => {
    const status = JSON.parse(cli('--json', 'status').stdout);
    expect(status.invoices).toEqual({ valid: 1, error: 1 });
    const review = JSON.parse(cli('--json', 'review').stdout);
    expect(review.invoices[0].issues.map((i) => i.rule_code)).toContain('TOTAL_MISMATCH');
  });

  it('exporta el CSV del mes', () => {
    const { code, stdout } = cli('export', '--month', '2026-10', '--csv');
    expect(code).toBe(0);
    expect(stdout).toContain('Exportadas 1 facturas');
    expect(fs.existsSync(path.join(workDir, 'exports', 'invoices-2026-10.csv'))).toBe(true);
  });

  it('config no revela secretos y doctor pasa sin conexión', () => {
    expect(cli('config').stdout).toContain('Proveedor de IA:      mock');
    const doctor = cli('doctor');
    expect(doctor.code).toBe(0);
    expect(doctor.stdout).toContain('✔ SQLite');
  });
});
