import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterEach, describe, expect, it } from 'vitest';
import { fetchMail } from '../../src/application/use-cases/fetch-mail.ts';
import { getInvoiceDetail } from '../../src/application/use-cases/review-invoice.ts';
import { runBot } from '../../src/application/use-cases/run-bot.ts';
import { createMailSource } from '../../src/infrastructure/mail/mail-factory.ts';
import type { MailEndpoints } from '../../src/infrastructure/mail/mail-factory.ts';
import { loadConfig } from '../../src/shared/config.ts';
import { ConfigError } from '../../src/shared/errors.ts';
import { startFakeGmail, startFakeGraph, startFakeImap } from '../helpers/fake-mail-servers.ts';
import { makeTestDeps } from '../helpers/context.ts';
import { sqlAll, sqlColumn } from '../helpers/db.ts';
import { mailbox, savedFiles } from '../helpers/mailbox.ts';

const FIXTURES = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../fixtures');
const CERT_FILE = path.join(FIXTURES, 'mail-app-cert.pem');
const CERTIFICATE = new crypto.X509Certificate(fs.readFileSync(CERT_FILE, 'utf8'));
const TENANT = '11111111-2222-3333-4444-555555555555';

const cleanup: (() => unknown)[] = [];
afterEach(async () => {
  for (const fn of cleanup.splice(0).reverse()) await fn();
});

async function setup(env: Record<string, string>, endpoints: MailEndpoints) {
  const ctx = await makeTestDeps({ env });
  cleanup.push(() => fs.rmSync(ctx.dir, { recursive: true, force: true }));
  const inbox = path.join(ctx.dir, 'bandeja');
  fs.mkdirSync(inbox);
  const source = (extra: Record<string, string> = {}) =>
    createMailSource(ctx.deps.config, { ...env, ...extra }, endpoints);
  return { ...ctx, inbox, mailDir: path.join(inbox, 'correo'), source };
}

const ORDER = {
  INVOICEBOT_MAIL_PROCESSED_FOLDER: 'Procesado',
  INVOICEBOT_MAIL_REVIEW_FOLDER: 'Revisar',
};

describe('Microsoft 365 en modo empresa', () => {
  const env = {
    INVOICEBOT_MAIL_PROVIDER: 'graph',
    INVOICEBOT_MAIL_MS_TENANT: TENANT,
    INVOICEBOT_MAIL_MS_CLIENT_ID: 'app-id',
    INVOICEBOT_MAIL_MS_CERT_FILE: CERT_FILE,
    INVOICEBOT_MAIL_USER: 'facturas@empresa.pe',
    ...ORDER,
  };

  it('se autentica con certificado, ordena el buzón y cada factura sabe de qué correo vino', async () => {
    const graph = await startFakeGraph({ certificate: CERTIFICATE });
    cleanup.push(graph.close);
    const t = await setup(env, graph.endpoints);
    graph.state.messages.push(...mailbox(t.dir));

    const result = await runBot({
      deps: t.deps,
      inboxDir: t.inbox,
      mail: { source: t.source(), dir: t.mailDir },
    });
    expect(result.status).toBe('success');
    expect(result.report.mail).toMatchObject({
      read: 3,
      saved: 2,
      files: 4,
      moved: 3,
      moveFailed: 0,
    });

    // Client assertion firmado con el certificado, para el endpoint de tokens del directorio.
    const [{ header, claims }] = graph.state.assertions;
    expect(header).toMatchObject({ alg: 'RS256', 'x5t#S256': expect.any(String) });
    expect(claims).toMatchObject({ iss: 'app-id', sub: 'app-id' });
    expect(claims.aud).toMatch(new RegExp(`/${TENANT}/oauth2/v2.0/token$`));
    expect(graph.requests.join('\n')).not.toContain('client_secret');

    // Con comprobantes a "Procesado"; sin ellos a "Revisar". Las carpetas se crean solas.
    expect(Object.fromEntries(graph.state.moved)).toEqual({
      m1: 'Procesado',
      m2: 'Procesado',
      m3: 'Revisar',
    });
    expect(
      await sqlAll(t.db, 'SELECT message_id, moved_to FROM mail_messages ORDER BY message_id'),
    ).toEqual([
      { message_id: 'm1', moved_to: 'Procesado' },
      { message_id: 'm2', moved_to: 'Procesado' },
      { message_id: 'm3', moved_to: 'Revisar' },
    ]);

    // Trazabilidad: la factura muestra el correo del que salió.
    const [first] = await sqlColumn<number>(t.db, 'SELECT id FROM invoices ORDER BY number');
    const detail = await getInvoiceDetail({ deps: t.deps, invoiceId: first! });
    expect(detail.mailOrigins).toEqual([
      expect.objectContaining({
        account: 'graph:facturas@empresa.pe',
        sender: 'facturacion@proveedor.pe',
        subject: 'Factura F001-00000001',
        moved_to: 'Procesado',
      }),
    ]);

    // Los movidos ya no están en la bandeja de entrada.
    const again = await fetchMail({ deps: t.deps, source: t.source(), dir: t.mailDir });
    expect(again).toMatchObject({ listed: 0, read: 0, moved: 0 });
  });

  it('sin permiso de escritura registra los mensajes igual y los mueve cuando se otorga', async () => {
    const graph = await startFakeGraph({ certificate: CERTIFICATE, writable: false });
    cleanup.push(graph.close);
    const t = await setup(env, graph.endpoints);
    graph.state.messages.push(...mailbox(t.dir));

    const first = await fetchMail({ deps: t.deps, source: t.source(), dir: t.mailDir });
    expect(first).toMatchObject({ read: 3, files: 4, moved: 0, moveFailed: 3, failed: 0 });
    expect(savedFiles(t.mailDir)).toHaveLength(4);

    // TI otorga Mail.ReadWrite: se mueven sin volver a descargarlos.
    graph.state.folders.length = 1;
    const writable = await startFakeGraph({ certificate: CERTIFICATE });
    cleanup.push(writable.close);
    writable.state.messages.push(...graph.state.messages);
    const source = createMailSource(t.deps.config, env, writable.endpoints);
    const second = await fetchMail({ deps: t.deps, source, dir: t.mailDir });
    expect(second).toMatchObject({ read: 0, moved: 3, moveFailed: 0 });
    expect(writable.requests.some((r) => r.includes('$value'))).toBe(false);
  });

  it('rechaza un certificado cuya clave no corresponde, antes de conectar', async () => {
    const { config } = await setup(
      { ...env, INVOICEBOT_MAIL_MS_CERT_FILE: path.join(FIXTURES, 'mail-app-cert-wrong-key.pem') },
      {},
    ).then((t) => t.deps);
    expect(() =>
      createMailSource(config, {
        INVOICEBOT_MAIL_MS_CERT_FILE: path.join(FIXTURES, 'mail-app-cert-wrong-key.pem'),
      }),
    ).toThrow(ConfigError);
    expect(() =>
      createMailSource(config, {
        INVOICEBOT_MAIL_MS_CERT_FILE: path.join(FIXTURES, 'mail-app-cert-wrong-key.pem'),
      }),
    ).toThrow(/no corresponde al certificado/);
    expect(() =>
      loadConfig({
        env: { ...env, INVOICEBOT_MAIL_MS_CLIENT_SECRET: 'x' },
        cwd: FIXTURES,
        loadEnvFile: false,
      }),
    ).toThrow(/CLIENT_SECRET o INVOICEBOT_MAIL_MS_CERT_FILE, no ambos/);
  });
});

describe('Google Workspace en modo empresa', () => {
  it('la cuenta de servicio pide gmail.modify y etiqueta los correos al ordenar el buzón', async () => {
    const gmail = await startFakeGmail({ account: 'facturas@empresa.pe' });
    cleanup.push(gmail.close);
    const { privateKey } = crypto.generateKeyPairSync('rsa', { modulusLength: 2048 });
    const keyDir = fs.mkdtempSync(path.join(FIXTURES, '..', '.tmp-sa-'));
    cleanup.push(() => fs.rmSync(keyDir, { recursive: true, force: true }));
    const keyFile = path.join(keyDir, 'cuenta-servicio.json');
    fs.writeFileSync(
      keyFile,
      JSON.stringify({
        client_email: 'invoicebot@empresa-proyecto.iam.gserviceaccount.com',
        private_key: privateKey.export({ type: 'pkcs8', format: 'pem' }),
      }),
    );
    const env = {
      INVOICEBOT_MAIL_PROVIDER: 'gmail',
      INVOICEBOT_MAIL_GOOGLE_SERVICE_ACCOUNT_FILE: keyFile,
      INVOICEBOT_MAIL_USER: 'facturas@empresa.pe',
      ...ORDER,
    };
    const t = await setup(env, gmail.endpoints);
    gmail.state.messages.push(...mailbox(t.dir));

    const summary = await fetchMail({ deps: t.deps, source: t.source(), dir: t.mailDir });
    expect(summary).toMatchObject({ account: 'gmail:facturas@empresa.pe', files: 4, moved: 3 });
    expect(gmail.state.jwtClaims[0]).toMatchObject({
      iss: 'invoicebot@empresa-proyecto.iam.gserviceaccount.com',
      sub: 'facturas@empresa.pe',
      scope: 'https://www.googleapis.com/auth/gmail.modify',
    });
    // Con el buzón ordenado se lee solo la bandeja de entrada.
    expect(gmail.state.queries[0]).toMatch(/^has:attachment in:inbox after:\d+$/);
    expect(Object.fromEntries(gmail.state.moved)).toEqual({
      m1: 'Procesado',
      m2: 'Procesado',
      m3: 'Revisar',
    });
    expect(gmail.state.labels.map((l) => l.name)).toEqual(['INBOX', 'Procesado', 'Revisar']);
    expect((await fetchMail({ deps: t.deps, source: t.source(), dir: t.mailDir })).listed).toBe(0);
  });
});

describe('IMAP con buzón ordenado', () => {
  it('abre la carpeta con escritura, crea las carpetas y mueve los mensajes', async () => {
    const t0 = await makeTestDeps();
    const messages = mailbox(t0.dir).map((m, i) => ({ ...m, id: String(i + 1), attachment: true }));
    const imap = await startFakeImap({ messages });
    cleanup.push(imap.close);
    const env = {
      INVOICEBOT_MAIL_PROVIDER: 'imap',
      INVOICEBOT_MAIL_IMAP_HOST: imap.host,
      INVOICEBOT_MAIL_IMAP_PORT: String(imap.port),
      INVOICEBOT_MAIL_IMAP_TLS: 'false',
      INVOICEBOT_MAIL_USER: 'facturas@empresa.pe',
      INVOICEBOT_MAIL_PASSWORD: 'clave-app',
      ...ORDER,
    };
    const t = await setup(env, {});
    const summary = await fetchMail({ deps: t.deps, source: t.source(), dir: t.mailDir });
    expect(summary).toMatchObject({ files: 4, moved: 3, moveFailed: 0 });
    expect(imap.state.commands.some((c) => c.toUpperCase().startsWith('SELECT'))).toBe(true);
    expect(Object.fromEntries(imap.state.moved)).toEqual({
      '1': 'Procesado',
      '2': 'Procesado',
      '3': 'Revisar',
    });
    expect(imap.state.commands.some((c) => /STORE|EXPUNGE/i.test(c))).toBe(false);
  });
});
