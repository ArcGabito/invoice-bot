import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import { scanInvoices } from '../../src/application/use-cases/scan-invoices.ts';
import { startReviewServer } from '../../src/web/review-server.ts';
import { sampleInvoiceLines, writeTextPdf } from '../helpers/pdf-builder.ts';
import { buildUblInvoice, writeUbl } from '../helpers/ubl-builder.ts';
import { makeTestDeps } from '../helpers/context.ts';
import { sqlColumn } from '../helpers/db.ts';

const cleanups: (() => unknown)[] = [];
afterEach(async () => {
  for (const fn of cleanups.splice(0).reverse()) await fn();
});

async function setup() {
  const env = await makeTestDeps();
  cleanups.push(() => {
    fs.rmSync(env.dir, { recursive: true, force: true });
  });
  const inbox = path.join(env.dir, 'in');
  fs.mkdirSync(inbox);
  writeTextPdf(path.join(inbox, 'total-mal.pdf'), sampleInvoiceLines({ total: '125.00' }));
  writeUbl(path.join(inbox, 'valida.xml'), buildUblInvoice({ id: 'F001-00000009' }));
  await scanInvoices({ root: inbox, deps: env.deps });
  const server = await startReviewServer({
    deps: env.deps,
    reviewer: 'ana',
    port: 0,
    token: 't'.repeat(48),
  });
  cleanups.push(() => server.close());
  const api = async (
    pathname: string,
    body?: unknown,
    { token = server.token }: { token?: string | null } = {},
  ) => {
    const res = await fetch(new URL(pathname, server.url), {
      method: body ? 'POST' : 'GET',
      headers: {
        ...(token ? { 'x-invoicebot-token': token } : {}),
        ...(body ? { 'content-type': 'application/json' } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
    });
    const data: any = await res.json().catch(() => null);
    return { status: res.status, headers: res.headers, data };
  };
  return { ...env, server, api };
}

/** Petición con una cabecera Host arbitraria, como haría un ataque de DNS rebinding. */
function requestWithHost(port, host) {
  return new Promise<number | undefined>((resolve, reject) => {
    const req = http.request({ host: '127.0.0.1', port, path: '/', headers: { host } }, (res) => {
      res.resume();
      resolve(res.statusCode);
    });
    req.on('error', reject);
    req.end();
  });
}

describe('pantalla de revisión', () => {
  it('sirve la página con una política de seguridad estricta y el revisor', async () => {
    const { server } = await setup();
    const res = await fetch(server.url);
    const html = await res.text();
    expect(res.status).toBe(200);
    const csp = res.headers.get('content-security-policy');
    expect(csp).toMatch(/default-src 'none'/);
    expect(csp).toMatch(/script-src 'nonce-[^']+'/);
    expect(csp).toMatch(/frame-ancestors 'none'/);
    expect(html).toContain('Revisando como <strong>ana</strong>');
    expect(html).not.toMatch(/ on[a-z]+="/); // sin manejadores en línea
  });

  it('solo atiende peticiones locales y con el token de la sesión', async () => {
    const { server, api } = await setup();
    expect(server.url).toMatch(/^http:\/\/127\.0\.0\.1:\d+\/$/);
    expect(await requestWithHost(server.port, 'evil.example:80')).toBe(403);
    expect(await requestWithHost(server.port, `localhost:${server.port}`)).toBe(200);
    expect((await api('/api/queue', null, { token: null })).status).toBe(401);
    expect((await api('/api/queue', null, { token: 'x'.repeat(48) })).status).toBe(401);
    // Las acciones exigen JSON (un formulario de otra web no sirve).
    const form = await fetch(new URL('/api/invoices/1/approve', server.url), {
      method: 'POST',
      headers: {
        'x-invoicebot-token': server.token,
        'content-type': 'application/x-www-form-urlencoded',
      },
      body: 'force=true',
    });
    expect(form.status).toBe(415);
  });

  it('lista la bandeja, corrige, aprueba y registra al revisor', async () => {
    const { api, db } = await setup();
    const queue = await api('/api/queue');
    expect(queue.data.counts).toMatchObject({ pending: 1, approved: 0, rejected: 0 });
    const [item] = queue.data.items;
    expect(item).toMatchObject({ validation_status: 'error', series: 'F001' });

    const corrected = await api(`/api/invoices/${item.invoice_id}/correct`, {
      changes: { total: '118.00' },
      note: 'Leído mal',
    });
    expect(corrected.data).toMatchObject({ validationStatus: 'valid' });

    const approved = await api(`/api/invoices/${item.invoice_id}/approve`, {});
    expect(approved.data).toMatchObject({ reviewStatus: 'approved' });
    expect((await api('/api/queue')).data.counts).toMatchObject({ pending: 0, approved: 1 });
    expect((await api('/api/queue?view=approved')).data.items[0]).toMatchObject({
      reviewed_by: 'ana',
    });

    const users = await sqlColumn(db, 'SELECT DISTINCT [user] FROM review_actions');
    expect(users).toEqual(['ana']);
  });

  it('devuelve los errores de negocio como 400 con un mensaje legible', async () => {
    const { api } = await setup();
    const [item] = (await api('/api/queue')).data.items;
    const res = await api(`/api/invoices/${item.invoice_id}/reject`, { reason: '' });
    expect(res.status).toBe(400);
    expect(res.data.error).toMatch(/motivo/);
    expect((await api('/api/invoices/999')).status).toBe(400);
    expect((await api('/api/no-existe')).status).toBe(404);
  });

  it('muestra el documento original: el PDF como PDF y el XML como texto', async () => {
    const { server, api } = await setup();
    const detailOf = async (file) => {
      for (const view of ['pending', 'approved']) {
        const items = (await api(`/api/queue?view=${view}`)).data.items;
        const found = items.find((i) => i.file_name === file);
        if (found) return found.invoice_id;
      }
      return null;
    };
    const pdfId = await detailOf('total-mal.pdf');
    const pdf = await fetch(
      new URL(`/api/invoices/${pdfId}/document?token=${server.token}`, server.url),
    );
    expect(pdf.headers.get('content-type')).toBe('application/pdf');
    expect((await pdf.arrayBuffer()).byteLength).toBeGreaterThan(100);
    expect((await fetch(new URL(`/api/invoices/${pdfId}/document`, server.url))).status).toBe(401);
  });
});
