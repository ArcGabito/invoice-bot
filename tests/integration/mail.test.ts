import fs from 'node:fs';
import path from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import { fetchMail } from '../../src/application/use-cases/fetch-mail.ts';
import { runBot } from '../../src/application/use-cases/run-bot.ts';
import { createMailSource } from '../../src/infrastructure/mail/mail-factory.ts';
import type { MailEndpoints } from '../../src/infrastructure/mail/mail-factory.ts';
import { readMailToken, writeMailToken } from '../../src/infrastructure/mail/token-store.ts';
import { buildMimeMessage, buildZip } from '../helpers/mail-builder.ts';
import { buildUblInvoice } from '../helpers/ubl-builder.ts';
import { sampleInvoiceLines, writeTextPdf } from '../helpers/pdf-builder.ts';
import { startFakeGmail, startFakeGraph, startFakeImap } from '../helpers/fake-mail-servers.ts';
import type { FakeMessage } from '../helpers/fake-mail-servers.ts';
import { makeTestDeps } from '../helpers/context.ts';
import type { TestContext } from '../helpers/context.ts';
import { sqlAll, sqlGet } from '../helpers/db.ts';

const cleanup: (() => unknown)[] = [];
afterEach(async () => {
  for (const fn of cleanup.splice(0).reverse()) await fn();
});

const daysAgo = (n: number) => new Date(Date.now() - n * 24 * 60 * 60 * 1000).toISOString();

/** PDF real (con texto) de una factura, como el que adjuntan los emisores. */
function invoicePdf(dir: string, number: string): Buffer {
  const file = path.join(dir, `tmp-${number}.pdf`);
  writeTextPdf(file, sampleInvoiceLines({ number }));
  const content = fs.readFileSync(file);
  fs.rmSync(file);
  return content;
}

/** Tres correos: XML + PDF sueltos, un ZIP con XML + PDF y uno sin comprobantes. */
function mailbox(dir: string): FakeMessage[] {
  return [
    {
      id: 'm1',
      receivedAt: daysAgo(3),
      raw: buildMimeMessage({
        subject: 'Factura F001-00000001',
        attachments: [
          {
            name: 'F001-00000001.xml',
            type: 'text/xml',
            content: buildUblInvoice({ id: 'F001-00000001' }),
          },
          {
            name: 'F001-00000001.pdf',
            type: 'application/pdf',
            content: invoicePdf(dir, '00000001'),
          },
        ],
      }),
    },
    {
      id: 'm2',
      receivedAt: daysAgo(2),
      raw: buildMimeMessage({
        subject: 'Comprobante electrónico',
        attachments: [
          {
            name: '20100047218-01-F001-00000002.zip',
            type: 'application/zip',
            content: buildZip({
              '20100047218-01-F001-00000002.xml': buildUblInvoice({ id: 'F001-00000002' }),
              '20100047218-01-F001-00000002.pdf': invoicePdf(dir, '00000002'),
            }),
          },
        ],
      }),
    },
    {
      id: 'm3',
      receivedAt: daysAgo(1),
      raw: buildMimeMessage({
        subject: 'Contrato',
        attachments: [{ name: 'contrato.docx', type: 'application/octet-stream', content: 'x' }],
      }),
    },
  ];
}

async function setup(env: Record<string, string>, endpoints: MailEndpoints) {
  const ctx: TestContext = await makeTestDeps({ env });
  cleanup.push(() => fs.rmSync(ctx.dir, { recursive: true, force: true }));
  const inbox = path.join(ctx.dir, 'bandeja');
  fs.mkdirSync(inbox);
  const source = () => createMailSource(ctx.deps.config, env, endpoints);
  return { ...ctx, inbox, mailDir: path.join(inbox, 'correo'), source };
}

const savedFiles = (dir: string) =>
  fs.existsSync(dir)
    ? (fs.readdirSync(dir, { recursive: true }) as string[])
        .filter((f) => path.extname(f))
        .map((f) => path.basename(f))
        .sort()
    : [];

describe('correo con la API de Gmail', () => {
  it('descarga los comprobantes una sola vez y el ciclo los procesa', async () => {
    const gmail = await startFakeGmail();
    cleanup.push(gmail.close);
    const env = {
      INVOICEBOT_MAIL_PROVIDER: 'gmail',
      INVOICEBOT_MAIL_GOOGLE_CLIENT_ID: 'cliente.apps.googleusercontent.com',
      INVOICEBOT_MAIL_GOOGLE_CLIENT_SECRET: 'secreto',
      INVOICEBOT_MAIL_REFRESH_TOKEN: 'rt-gmail',
    };
    const t = await setup(env, gmail.endpoints);
    gmail.state.messages.push(...mailbox(t.dir));

    const result = await runBot({
      deps: t.deps,
      inboxDir: t.inbox,
      mail: { source: t.source(), dir: t.mailDir },
    });
    expect(result.status).toBe('success');
    expect(result.report.mail).toEqual({
      account: 'gmail:pcjhonny13@gmail.com',
      listed: 3,
      read: 3,
      saved: 2,
      files: 4,
      noAttachments: 1,
      failed: 0,
      remaining: 0,
    });
    expect(gmail.state.queries[0]).toMatch(/^has:attachment -in:sent -in:drafts after:\d+$/);
    expect(savedFiles(t.mailDir)).toEqual([
      '20100047218-01-F001-00000002.pdf',
      '20100047218-01-F001-00000002.xml',
      'F001-00000001.pdf',
      'F001-00000001.xml',
    ]);
    // El ciclo escanea lo descargado: cada PDF se vincula a su XML, sin IA.
    expect(result.report.scan).toMatchObject({ found: 4, extracted: 2, fromXml: 2 });
    expect(await sqlAll(t.db, 'SELECT number, data_source FROM invoices ORDER BY number')).toEqual([
      { number: '00000001', data_source: 'xml' },
      { number: '00000002', data_source: 'xml' },
    ]);
    expect(
      await sqlAll(
        t.db,
        'SELECT message_id, status, files, subject FROM mail_messages ORDER BY message_id',
      ),
    ).toEqual([
      { message_id: 'm1', status: 'saved', files: 2, subject: 'Factura F001-00000001' },
      { message_id: 'm2', status: 'saved', files: 2, subject: 'Comprobante electrónico' },
      { message_id: 'm3', status: 'no_attachments', files: 0, subject: 'Contrato' },
    ]);

    // Segundo ciclo: nada nuevo que descargar.
    const before = gmail.requests.filter((r) => r.includes('format=raw')).length;
    const again = await runBot({
      deps: t.deps,
      inboxDir: t.inbox,
      mail: { source: t.source(), dir: t.mailDir },
    });
    expect(again.report.mail).toMatchObject({ listed: 3, read: 0, files: 0 });
    expect(gmail.requests.filter((r) => r.includes('format=raw')).length).toBe(before);
  });

  it('con la carpeta del correo fuera de la de entrada (Docker), run también la escanea', async () => {
    const gmail = await startFakeGmail();
    cleanup.push(gmail.close);
    const env = {
      INVOICEBOT_MAIL_PROVIDER: 'gmail',
      INVOICEBOT_MAIL_GOOGLE_CLIENT_ID: 'cliente',
      INVOICEBOT_MAIL_GOOGLE_CLIENT_SECRET: 'secreto',
      INVOICEBOT_MAIL_REFRESH_TOKEN: 'rt-gmail',
      INVOICEBOT_MAIL_DIR: './data/correo',
    };
    const t = await setup(env, gmail.endpoints);
    gmail.state.messages.push(...mailbox(t.dir).slice(0, 1));
    const dir = t.deps.config.mail.dir!;
    expect(dir).toBe(path.join(t.dir, 'data', 'correo'));
    const result = await runBot({
      deps: t.deps,
      inboxDir: t.inbox,
      mail: { source: t.source(), dir },
    });
    expect(result.report.scan).toMatchObject({ found: 2, extracted: 1, fromXml: 1 });
    expect(savedFiles(t.mailDir)).toEqual([]);
    expect(savedFiles(dir)).toEqual(['F001-00000001.pdf', 'F001-00000001.xml']);
  });

  it('reintenta un mensaje que falló y se detiene si el acceso venció', async () => {
    const gmail = await startFakeGmail({ failing: new Set(['m2']) });
    cleanup.push(gmail.close);
    const env = {
      INVOICEBOT_MAIL_PROVIDER: 'gmail',
      INVOICEBOT_MAIL_GOOGLE_CLIENT_ID: 'cliente',
      INVOICEBOT_MAIL_GOOGLE_CLIENT_SECRET: 'secreto',
      INVOICEBOT_MAIL_REFRESH_TOKEN: 'rt-gmail',
    };
    const t = await setup(env, gmail.endpoints);
    gmail.state.messages.push(...mailbox(t.dir));

    const first = await fetchMail({ deps: t.deps, source: t.source(), dir: t.mailDir });
    expect(first).toMatchObject({ read: 2, failed: 1 });
    expect(
      await sqlGet(
        t.db,
        "SELECT status, attempts, error_message FROM mail_messages WHERE message_id = 'm2'",
      ),
    ).toEqual({
      status: 'failed',
      attempts: 1,
      error_message: 'Gmail respondió 500: Backend Error',
    });

    gmail.state.failing.clear();
    const second = await fetchMail({ deps: t.deps, source: t.source(), dir: t.mailDir });
    expect(second).toMatchObject({ read: 1, saved: 1, files: 2, failed: 0 });
    expect(
      await sqlGet(t.db, "SELECT status, attempts FROM mail_messages WHERE message_id = 'm2'"),
    ).toEqual({
      status: 'saved',
      attempts: 2,
    });

    // Token revocado: el ciclo sigue (escanea lo que haya) pero queda parcial y lo explica.
    const revoked = { ...env, INVOICEBOT_MAIL_REFRESH_TOKEN: 'revocado' };
    const t2 = { ...t, source: () => createMailSource(t.deps.config, revoked, gmail.endpoints) };
    const run = await runBot({
      deps: t.deps,
      inboxDir: t.inbox,
      mail: { source: t2.source(), dir: t.mailDir },
    });
    expect(run.status).toBe('partial');
    expect(run.report.mail).toEqual({
      error: expect.stringMatching(/expired or revoked.*invoicebot mail login/),
    });
  });
});

describe('correo con Microsoft Graph', () => {
  it('Microsoft 365 con permiso de aplicación lee el buzón de la empresa, en páginas', async () => {
    const graph = await startFakeGraph();
    cleanup.push(graph.close);
    const env = {
      INVOICEBOT_MAIL_PROVIDER: 'graph',
      INVOICEBOT_MAIL_MS_TENANT: '11111111-2222-3333-4444-555555555555',
      INVOICEBOT_MAIL_MS_CLIENT_ID: 'app-id',
      INVOICEBOT_MAIL_MS_CLIENT_SECRET: 'secreto',
      INVOICEBOT_MAIL_USER: 'facturas@empresa.pe',
    };
    const t = await setup(env, graph.endpoints);
    graph.state.messages.push(...mailbox(t.dir), {
      id: 'antiguo',
      receivedAt: daysAgo(30),
      raw: buildMimeMessage(),
    });

    const summary = await fetchMail({ deps: t.deps, source: t.source(), dir: t.mailDir });
    expect(summary).toMatchObject({
      account: 'graph:facturas@empresa.pe',
      listed: 3,
      saved: 2,
      files: 4,
    });
    // Tres mensajes en la ventana de 7 días: dos páginas.
    expect(graph.requests.filter((r) => r.includes('/mailFolders/inbox/messages'))).toHaveLength(2);
    expect(graph.requests).toContain(
      'POST /11111111-2222-3333-4444-555555555555/oauth2/v2.0/token',
    );
    expect(savedFiles(t.mailDir)).toHaveLength(4);
  });

  it('Outlook.com con inicio de sesión guarda el refresh token renovado y lee una carpeta propia', async () => {
    const graph = await startFakeGraph();
    cleanup.push(graph.close);
    const env = {
      INVOICEBOT_MAIL_PROVIDER: 'graph',
      INVOICEBOT_MAIL_MS_TENANT: 'consumers',
      INVOICEBOT_MAIL_MS_CLIENT_ID: 'app-id',
      INVOICEBOT_MAIL_FOLDER: 'Facturas',
    };
    const t = await setup(env, graph.endpoints);
    writeMailToken(t.deps.config.mail.tokenFile, {
      provider: 'graph',
      clientId: 'app-id',
      account: 'gabicho_05@outlook.com',
      refreshToken: 'rt-ms-1',
    });
    graph.state.messages.push(...mailbox(t.dir));

    const summary = await fetchMail({ deps: t.deps, source: t.source(), dir: t.mailDir });
    expect(summary).toMatchObject({ account: 'graph:gabicho_05@outlook.com', files: 4 });
    expect(
      graph.requests.some((r) => r.startsWith('GET /v1.0/me/mailFolders/facturas-id/messages')),
    ).toBe(true);
    // Microsoft entregó otro refresh token: queda guardado para el próximo inicio.
    expect(readMailToken(t.deps.config.mail.tokenFile)).toMatchObject({
      refreshToken: 'rt-ms-2',
      account: 'gabicho_05@outlook.com',
    });
    expect((await fetchMail({ deps: t.deps, source: t.source(), dir: t.mailDir })).read).toBe(0);
  });
});

describe('correo por IMAP', () => {
  it('lee en solo lectura los mensajes con adjuntos', async () => {
    const t0 = await makeTestDeps();
    const messages = mailbox(t0.dir).map((m, i) => ({ ...m, id: String(i + 1), attachment: true }));
    messages.push({ id: '4', receivedAt: daysAgo(1), raw: buildMimeMessage(), attachment: false });
    const imap = await startFakeImap({ messages });
    cleanup.push(imap.close);
    const env = {
      INVOICEBOT_MAIL_PROVIDER: 'imap',
      INVOICEBOT_MAIL_IMAP_HOST: imap.host,
      INVOICEBOT_MAIL_IMAP_PORT: String(imap.port),
      INVOICEBOT_MAIL_IMAP_TLS: 'false',
      INVOICEBOT_MAIL_USER: 'facturas@empresa.pe',
      INVOICEBOT_MAIL_PASSWORD: 'clave-app',
    };
    const t = await setup(env, {});
    const summary = await fetchMail({ deps: t.deps, source: t.source(), dir: t.mailDir });
    expect(summary).toMatchObject({
      account: 'imap:facturas@empresa.pe',
      listed: 3,
      saved: 2,
      files: 4,
      noAttachments: 1,
    });
    expect(imap.state.commands.some((c) => c.toUpperCase().startsWith('EXAMINE'))).toBe(true);
    expect(imap.state.commands.some((c) => /STORE|EXPUNGE|COPY|MOVE/i.test(c))).toBe(false);
    expect(await sqlAll(t.db, 'SELECT message_id FROM mail_messages ORDER BY message_id')).toEqual([
      { message_id: '7:1' },
      { message_id: '7:2' },
      { message_id: '7:3' },
    ]);

    const bad = createMailSource(t.deps.config, { ...env, INVOICEBOT_MAIL_PASSWORD: 'otra' });
    await expect(fetchMail({ deps: t.deps, source: bad, dir: t.mailDir })).rejects.toThrow(
      /rechazó el usuario o la contraseña.*contraseña de aplicación/,
    );
  });
});
