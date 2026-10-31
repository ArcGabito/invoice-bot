import { execFile } from 'node:child_process';
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { buildUblInvoice, writeUbl } from '../helpers/ubl-builder.js';
import { makeTempDir } from '../helpers/context.js';

const BIN = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../bin/invoicebot.js');

/**
 * SUNAT falso en 127.0.0.1 que imita el servicio de Consulta Integrada:
 * el comprobante número 2 está anulado y el resto, aceptados.
 */
function startFakeSunat() {
  const requests = [];
  const server = http.createServer((req, res) => {
    let body = '';
    req.on('data', (chunk) => (body += chunk));
    req.on('end', () => {
      requests.push({ url: req.url, auth: req.headers.authorization, body });
      res.setHeader('content-type', 'application/json');
      if (req.url === '/auth/cid/oauth2/token/') {
        res.end(JSON.stringify({ access_token: 'tok', token_type: 'JWT', expires_in: 3600 }));
      } else if (req.url === '/api/20555555551/validarcomprobante') {
        const { numero } = JSON.parse(body);
        const estadoCp = numero === 2 ? '2' : '1';
        res.end(
          JSON.stringify({ success: true, data: { estadoCp, estadoRuc: '00', condDomiRuc: '00' } }),
        );
      } else {
        res.statusCode = 404;
        res.end('{}');
      }
    });
  });
  return new Promise((resolve) =>
    server.listen(0, '127.0.0.1', () => resolve({ server, requests, port: server.address().port })),
  );
}

// El servidor vive en este proceso: la CLI debe lanzarse de forma asíncrona.
function cli(cwd, env, ...args) {
  return new Promise((resolve) => {
    execFile(
      process.execPath,
      [BIN, ...args],
      { cwd, env, encoding: 'utf8' },
      (error, stdout, stderr) => resolve({ code: error ? error.code : 0, stdout, stderr }),
    );
  });
}

describe('verificación en SUNAT configurable', { timeout: 60_000 }, () => {
  let fake;
  let workDir;
  let env;

  beforeAll(async () => {
    fake = await startFakeSunat();
    workDir = makeTempDir('invoicebot-e2e-sunat-');
    fs.mkdirSync(path.join(workDir, 'facturas'));
    writeUbl(path.join(workDir, 'facturas', 'a.xml'), buildUblInvoice({ id: 'F001-00000001' }));
    writeUbl(path.join(workDir, 'facturas', 'b.xml'), buildUblInvoice({ id: 'F001-00000002' }));
    env = {
      ...process.env,
      INVOICEBOT_AI_PROVIDER: 'mock',
      INVOICEBOT_LOG_LEVEL: 'silent',
      INVOICEBOT_SUNAT_RUC: '20555555551',
      INVOICEBOT_SUNAT_CLIENT_ID: 'cid',
      INVOICEBOT_SUNAT_CLIENT_SECRET: 'valor-secreto-xyz',
      INVOICEBOT_SUNAT_AUTH_URL: `http://127.0.0.1:${fake.port}/auth`,
      INVOICEBOT_SUNAT_API_URL: `http://127.0.0.1:${fake.port}/api`,
    };
  });

  afterAll(() => {
    fake.server.close();
    fs.rmSync(workDir, { recursive: true, force: true });
  });

  it('con INVOICEBOT_SUNAT_ENABLED=false no contacta a SUNAT', async () => {
    const { code, stdout } = await cli(
      workDir,
      { ...env, INVOICEBOT_SUNAT_ENABLED: 'false' },
      'doctor',
    );
    expect(code).toBe(0);
    expect(stdout).toContain('– API SUNAT: desactivada');
    expect(fake.requests).toHaveLength(0);
  });

  it('con verificación automática, scan consulta SUNAT al terminar', async () => {
    const autoEnv = {
      ...env,
      INVOICEBOT_SUNAT_ENABLED: 'true',
      INVOICEBOT_SUNAT_VERIFY_ON_SCAN: 'true',
    };
    const { code, stdout } = await cli(workDir, autoEnv, '--json', 'scan', './facturas');
    const result = JSON.parse(stdout);

    expect(code).toBe(3); // una factura anulada
    expect(result.fromXml).toBe(2);
    expect(result.sunat).toMatchObject({ checked: 2, accepted: 1, voided: 1 });
    const queries = fake.requests.filter((r) => r.url.endsWith('/validarcomprobante'));
    expect(queries).toHaveLength(2);
    expect(queries[0].auth).toBe('Bearer tok');

    // --no-verify-sunat lo desactiva para una ejecución concreta.
    const again = await cli(workDir, autoEnv, '--json', 'scan', './facturas', '--no-verify-sunat');
    expect(JSON.parse(again.stdout).sunat).toBeNull();
  });

  it('verify-sunat y doctor usan la configuración activada', async () => {
    const onEnv = { ...env, INVOICEBOT_SUNAT_ENABLED: 'true' };
    const verify = await cli(workDir, onEnv, 'verify-sunat', '--force');
    expect(verify.stdout).toContain('Anulados: 1');
    const doctor = await cli(workDir, onEnv, 'doctor');
    expect(doctor.stdout).toContain('✔ API SUNAT: credenciales válidas para el RUC 20555555551');
    const config = await cli(workDir, onEnv, 'config');
    expect(config.stdout).toContain('Verificación SUNAT:   activada, manual (verify-sunat)');
    expect(config.stdout).not.toContain('valor-secreto-xyz');
  });
});
