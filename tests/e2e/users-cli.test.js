import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterAll, describe, expect, it } from 'vitest';
import { makeTempDir } from '../helpers/context.js';

const BIN = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../bin/invoicebot.js');
const workDir = makeTempDir('invoicebot-e2e-users-');
afterAll(() => fs.rmSync(workDir, { recursive: true, force: true }));

const baseEnv = {
  ...process.env,
  INVOICEBOT_AI_PROVIDER: 'mock',
  INVOICEBOT_LOG_LEVEL: 'silent',
  INVOICEBOT_DATA_DIR: './data',
};

function cli(env, args, input) {
  const result = spawnSync(process.execPath, [BIN, ...args], {
    cwd: workDir,
    encoding: 'utf8',
    env: { ...baseEnv, ...env },
    input,
  });
  return { code: result.status, stdout: result.stdout, stderr: result.stderr };
}

describe('usuarios y pantalla de revisión en red desde la CLI', { timeout: 60_000 }, () => {
  const network = { INVOICEBOT_SERVE_HOST: '0.0.0.0' };

  it('en red no arranca sin usuarios', () => {
    const { code, stderr } = cli(network, ['serve']);
    expect(code).toBe(2);
    expect(stderr).toContain('invoicebot user add');
  });

  it('da de alta, lista y desactiva usuarios', () => {
    const added = cli({}, ['--json', 'user', 'add', 'Ana.Perez', '--name', 'Ana Pérez']);
    expect(added.code).toBe(0);
    const { user, password } = JSON.parse(added.stdout);
    expect(user).toMatchObject({ username: 'ana.perez', display_name: 'Ana Pérez', active: 1 });
    expect(password).toMatch(/^[\w]{5}(-[\w]{5}){3}$/);

    expect(cli({}, ['user', 'add', 'luis', '--password-stdin'], 'clave-de-luis-2026\n').code).toBe(
      0,
    );
    expect(cli({}, ['user', 'add', 'eva', '--password-stdin'], 'corta\n')).toMatchObject({
      code: 2,
      stderr: expect.stringContaining('al menos 12'),
    });
    expect(cli({}, ['user', 'add', 'ana.perez']).code).toBe(2);
    expect(cli({}, ['user', 'disable', 'luis']).code).toBe(0);

    const list = cli({}, ['user', 'list']).stdout;
    expect(list).toMatch(/✔ ana\.perez\s+Ana Pérez\s+último acceso: nunca/);
    expect(list).toMatch(/– luis/);
    // La contraseña nunca se guarda en claro.
    const db = fs.readFileSync(path.join(workDir, 'data', 'invoicebot.db'));
    expect(db.includes('clave-de-luis-2026')).toBe(false);
  });

  it('en red exige HTTPS (o un proxy declarado) y un certificado legible', () => {
    const noTls = cli(network, ['serve']);
    expect(noTls.code).toBe(2);
    expect(noTls.stderr).toContain('INVOICEBOT_SERVE_TLS_PFX');

    const missing = cli({ ...network, INVOICEBOT_SERVE_TLS_PFX: './no-existe.pfx' }, ['serve']);
    expect(missing.code).toBe(2);
    expect(missing.stderr).toContain('No se pudo leer INVOICEBOT_SERVE_TLS_PFX');

    fs.writeFileSync(path.join(workDir, 'roto.pfx'), 'no es un certificado');
    const broken = cli({ ...network, INVOICEBOT_SERVE_TLS_PFX: './roto.pfx' }, ['serve']);
    expect(broken.code).toBe(2);
    expect(broken.stderr).toContain('El certificado HTTPS no es válido');

    const cert = cli({ INVOICEBOT_SERVE_TLS_CERT: './a.pem' }, ['config']);
    expect(cert.code).toBe(2);
    expect(cert.stderr).toContain('van juntos');
  });

  it('doctor y config muestran el modo de la pantalla de revisión', () => {
    const env = { ...network, INVOICEBOT_SERVE_ALLOW_HTTP: 'true' };
    expect(cli(env, ['config']).stdout).toContain(
      'Pantalla de revisión: en red (0.0.0.0:4317) · HTTP sin cifrar',
    );
    const doctor = cli({ ...env, INVOICEBOT_OCR_ENGINE: 'none' }, ['doctor', '--offline']);
    expect(doctor.stdout).toContain('✔ Pantalla de revisión en red: 1 usuario(s) activo(s)');
    expect(cli({}, ['config']).stdout).toContain('Pantalla de revisión: solo este equipo');
  });
});
