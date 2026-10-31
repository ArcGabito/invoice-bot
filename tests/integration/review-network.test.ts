import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import http from 'node:http';
import https from 'node:https';
import path from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import { scanInvoices } from '../../src/application/use-cases/scan-invoices.ts';
import { addUser, resetPassword, setUserActive } from '../../src/application/use-cases/users.ts';
import { UserRepository } from '../../src/infrastructure/database/repositories/user-repository.ts';
import { startReviewServer } from '../../src/web/review-server.ts';
import { sampleInvoiceLines, writeTextPdf } from '../helpers/pdf-builder.ts';
import { makeTempDir, makeTestDeps } from '../helpers/context.ts';
import { sqlValue, sqlColumn } from '../helpers/db.ts';

const PASSWORD = 'clave-de-ana-2026';
const cleanups: (() => unknown)[] = [];
afterEach(async () => {
  for (const fn of cleanups.splice(0).reverse()) await fn();
});

/** Petición HTTP(S) de bajo nivel: permite fijar cookies y confiar en un certificado propio. */
interface RequestOptions {
  method?: string;
  headers?: Record<string, string>;
  body?: string;
  ca?: Buffer;
}

interface RawResponse {
  status: number | undefined;
  headers: http.IncomingHttpHeaders;
  text: string;
  json: any;
}

function request(
  baseUrl: string,
  pathname: string,
  { method = 'GET', headers = {}, body, ca }: RequestOptions = {},
): Promise<RawResponse> {
  const url = new URL(pathname, baseUrl);
  const client = url.protocol === 'https:' ? https : http;
  return new Promise((resolve, reject) => {
    const req = client.request(url, { method, headers, ca }, (res) => {
      const chunks: Buffer[] = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => {
        const text = Buffer.concat(chunks).toString('utf8');
        let json = null;
        try {
          json = JSON.parse(text);
        } catch {
          // HTML o vacío
        }
        resolve({ status: res.statusCode, headers: res.headers, text, json });
      });
    });
    req.on('error', reject);
    req.end(body);
  });
}

async function setup({ tls = null }: { tls?: { key: Buffer; cert: Buffer } | null } = {}) {
  const env = await makeTestDeps();
  cleanups.push(() => {
    fs.rmSync(env.dir, { recursive: true, force: true });
  });
  const inbox = path.join(env.dir, 'in');
  fs.mkdirSync(inbox);
  writeTextPdf(path.join(inbox, 'total-mal.pdf'), sampleInvoiceLines({ total: '125.00' }));
  await scanInvoices({ root: inbox, deps: env.deps });

  const users = new UserRepository(env.db);
  await addUser({ users, username: 'ana', displayName: 'Ana Pérez', password: PASSWORD });
  const server = await startReviewServer({
    deps: env.deps,
    host: '127.0.0.1',
    port: 0,
    auth: { users, sessionHours: 8 },
    tls,
  });
  cleanups.push(() => server.close());

  const ca = tls?.cert;
  const call = (pathname: string, opts: RequestOptions = {}) =>
    request(server.url, pathname, { ca, ...opts });
  const login = (username, password) =>
    call('/login', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ username, password }),
    });
  /** Entra y devuelve la cookie y el token antifalsificación de la página. */
  const session = async (username = 'ana', password = PASSWORD) => {
    const res = await login(username, password);
    expect(res.status).toBe(200);
    const setCookie = res.headers['set-cookie']![0]!;
    const cookie = setCookie.split(';')[0]!;
    const page = await call('/', { headers: { cookie } });
    const csrf = /name="invoicebot-token" content="([^"]+)"/.exec(page.text)![1]!;
    const api = (pathname: string, body?: unknown) =>
      call(pathname, {
        method: body ? 'POST' : 'GET',
        headers: {
          cookie,
          'x-invoicebot-token': csrf,
          ...(body ? { 'content-type': 'application/json' } : {}),
        },
        body: body ? JSON.stringify(body) : undefined,
      });
    return { cookie, csrf, page, api, setCookie };
  };
  return { ...env, users, server, call, login, session };
}

describe('pantalla de revisión en red', () => {
  it('no se abre a la red sin inicio de sesión', async () => {
    const { deps, dir } = await makeTestDeps();
    cleanups.push(() => {
      fs.rmSync(dir, { recursive: true, force: true });
    });
    await expect(startReviewServer({ deps, host: '0.0.0.0', port: 0 })).rejects.toThrow(
      /inicio de sesión/,
    );
  });

  it('sin sesión muestra la pantalla de acceso y no deja usar la API', async () => {
    const { call } = await setup();
    const page = await call('/');
    expect(page.status).toBe(200);
    expect(page.text).toContain('autocomplete="current-password"');
    expect(page.text).not.toContain('Revisando como');
    expect(page.headers['content-security-policy']).toMatch(/default-src 'none'/);
    expect((await call('/api/queue')).status).toBe(401);
  });

  it('cada persona entra con su usuario y firma sus revisiones', async () => {
    const { session, db } = await setup();
    const { page, api, setCookie } = await session('ANA');
    expect(setCookie).toMatch(
      /^invoicebot_session=[\w-]{43}; Path=\/; HttpOnly; SameSite=Strict; Max-Age=28800$/,
    );
    expect(page.text).toContain('Revisando como <strong>Ana Pérez</strong>');
    expect(page.text).toContain('Cerrar sesión');

    const [item] = (await api('/api/queue')).json.items;
    expect((await api('/api/meta')).json.reviewer).toBe('ana');
    await api(`/api/invoices/${item.invoice_id}/correct`, { changes: { total: '118.00' } });
    expect((await api(`/api/invoices/${item.invoice_id}/approve`, {})).status).toBe(200);
    expect(await sqlColumn(db, 'SELECT DISTINCT [user] FROM review_actions')).toEqual(['ana']);
    expect(await sqlValue(db, 'SELECT reviewed_by FROM invoices')).toBe('ana');
  });

  it('exige el token antifalsificación además de la cookie', async () => {
    const { session, call } = await setup();
    const { cookie, csrf } = await session();
    expect((await call('/api/queue', { headers: { cookie } })).status).toBe(403);
    expect(
      (await call('/api/queue', { headers: { cookie, 'x-invoicebot-token': 'x'.repeat(48) } }))
        .status,
    ).toBe(403);
    // La cookie de otra sesión no sirve con este token.
    expect(
      (
        await call('/api/queue', {
          headers: { cookie: 'invoicebot_session=otra', 'x-invoicebot-token': csrf },
        })
      ).status,
    ).toBe(401);
  });

  it('rechaza contraseñas incorrectas y bloquea tras cinco intentos', async () => {
    const { login } = await setup();
    const wrong = await login('ana', 'contraseña-mala');
    expect(wrong.status).toBe(401);
    expect(wrong.json.error).toBe('Usuario o contraseña incorrectos');
    expect((await login('nadie', PASSWORD)).json.error).toBe('Usuario o contraseña incorrectos');
    for (let i = 0; i < 4; i++) await login('ana', 'contraseña-mala');
    const locked = await login('ana', PASSWORD);
    expect(locked.status).toBe(429);
    expect(locked.json.error).toMatch(/15 minutos/);
  });

  it('cerrar sesión, cambiar la contraseña o desactivar al usuario invalidan la sesión', async () => {
    const { session, users, call } = await setup();

    const first = await session();
    const out = await first.api('/logout', {});
    expect(out.status).toBe(200);
    expect(out.headers['set-cookie']![0]).toMatch(/Max-Age=0/);
    expect((await first.api('/api/queue')).status).toBe(401);

    const second = await session();
    const { password } = await resetPassword({ users, username: 'ana' });
    expect((await second.api('/api/queue')).status).toBe(401);
    expect((await call('/', { headers: { cookie: second.cookie } })).text).not.toContain(
      'Revisando como',
    );

    const third = await session('ana', password);
    await setUserActive({ users, username: 'ana', active: false });
    expect((await third.api('/api/queue')).status).toBe(401);
  });

  it('la sesión sigue abierta si se reinicia la pantalla de revisión', async () => {
    const { session, server, deps, users } = await setup();
    const { cookie, csrf } = await session();
    await server.close();

    const again = await startReviewServer({
      deps,
      host: '127.0.0.1',
      port: 0,
      auth: { users, sessionHours: 8 },
    });
    cleanups.push(() => again.close());
    const res = await request(again.url, '/api/queue', {
      headers: { cookie, 'x-invoicebot-token': csrf },
    });
    expect(res.status).toBe(200);
  });
});

/** Certificado autofirmado de prueba con openssl (si está instalado). */
function makeCertificate() {
  const dir = makeTempDir('invoicebot-tls-');
  const key = path.join(dir, 'key.pem');
  const cert = path.join(dir, 'cert.pem');
  const result = spawnSync(
    'openssl',
    [
      'req',
      '-x509',
      '-newkey',
      'rsa:2048',
      '-nodes',
      '-days',
      '1',
      '-keyout',
      key,
      '-out',
      cert,
      '-subj',
      '/CN=localhost',
      '-addext',
      'subjectAltName=DNS:localhost,IP:127.0.0.1',
    ],
    { encoding: 'utf8' },
  );
  if (result.status !== 0) {
    fs.rmSync(dir, { recursive: true, force: true });
    return null;
  }
  const tls = { key: fs.readFileSync(key), cert: fs.readFileSync(cert) };
  fs.rmSync(dir, { recursive: true, force: true });
  return tls;
}

const certificate = makeCertificate();

describe.skipIf(!certificate)('pantalla de revisión en red con HTTPS', () => {
  it('sirve HTTPS con cookie __Host- segura y HSTS', async () => {
    const { server, session } = await setup({ tls: certificate });
    expect(server.url).toMatch(/^https:\/\/127\.0\.0\.1:\d+\/$/);
    const { setCookie, page, api } = await session();
    expect(setCookie).toMatch(
      /^__Host-invoicebot=[\w-]+; Path=\/; HttpOnly; SameSite=Strict; Secure;/,
    );
    expect(page.headers['strict-transport-security']).toBe('max-age=31536000');
    expect((await api('/api/queue')).status).toBe(200);
  });
});
