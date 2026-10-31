import crypto from 'node:crypto';
import fs from 'node:fs';
import http from 'node:http';
import {
  approveInvoice,
  correctInvoice,
  getInvoiceDetail,
  rejectInvoice,
  reopenInvoice,
  resolveIssue,
} from '../application/use-cases/review-invoice.js';
import { getReviewQueue } from '../application/use-cases/review-queue.js';
import { DOCUMENT_TYPES } from '../domain/invoice.js';
import { CORRECTABLE_FIELDS, REVIEW_STATUS } from '../domain/review.js';
import { InvoiceBotError } from '../shared/errors.js';
import { renderReviewPage } from './review-page.js';

const MAX_BODY_BYTES = 64 * 1024;

class HttpError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

function send(res, status, body, headers = {}) {
  res.writeHead(status, {
    'x-content-type-options': 'nosniff',
    'referrer-policy': 'no-referrer',
    'cache-control': 'no-store',
    ...headers,
  });
  res.end(body);
}

const sendJson = (res, status, data) =>
  send(res, status, JSON.stringify(data), { 'content-type': 'application/json; charset=utf-8' });

async function readJson(req) {
  if (!/^application\/json\b/.test(req.headers['content-type'] ?? '')) {
    throw new HttpError(415, 'Se esperaba JSON');
  }
  let size = 0;
  const chunks = [];
  for await (const chunk of req) {
    size += chunk.length;
    if (size > MAX_BODY_BYTES) throw new HttpError(413, 'Petición demasiado grande');
    chunks.push(chunk);
  }
  try {
    return chunks.length ? JSON.parse(Buffer.concat(chunks).toString('utf8')) : {};
  } catch {
    throw new HttpError(400, 'JSON mal formado');
  }
}

const safeEqual = (a, b) =>
  typeof a === 'string' &&
  a.length === b.length &&
  crypto.timingSafeEqual(Buffer.from(a), Buffer.from(b));

/**
 * Pantalla de revisión local. Escucha solo en 127.0.0.1 y exige:
 * - una cabecera Host local (evita que una web externa la use mediante DNS rebinding);
 * - el token aleatorio de esta sesión en cada llamada a /api (cabecera
 *   x-invoicebot-token, o ?token= para ver el documento original).
 * Las acciones se firman con `reviewer`, la persona que inició `serve`.
 *
 * @returns {Promise<{ url: string, port: number, token: string, close: () => Promise<void> }>}
 */
export async function startReviewServer({
  deps,
  reviewer,
  port = 4317,
  token = crypto.randomBytes(24).toString('hex'),
}) {
  const { repository, logger } = deps;
  let actualPort = port;

  const routes = [
    [
      'GET',
      /^\/api\/meta$/,
      () => ({
        reviewer,
        fields: CORRECTABLE_FIELDS,
        documentTypes: DOCUMENT_TYPES,
      }),
    ],
    [
      'GET',
      /^\/api\/queue$/,
      (_m, _body, url) => {
        const view = url.searchParams.get('view') ?? 'pending';
        const summary = repository.getStatusSummary();
        const counts = {
          pending: repository.listInvoicesNeedingReview().length,
          approved: summary.review.approved ?? 0,
          rejected: summary.review.rejected ?? 0,
          failed: summary.documents.failed ?? 0,
          needsOcr: summary.documents.needs_ocr ?? 0,
        };
        if (view === 'pending') {
          const queue = getReviewQueue({ deps });
          return {
            view,
            counts,
            items: queue.invoices,
            failed: queue.failed,
            needsOcr: queue.needsOcr,
          };
        }
        if (!Object.values(REVIEW_STATUS).includes(view))
          throw new HttpError(400, 'Vista desconocida');
        return { view, counts, items: repository.listInvoicesByReviewStatus(view) };
      },
    ],
    ['GET', /^\/api\/invoices\/(\d+)$/, (m) => getInvoiceDetail({ deps, invoiceId: m[1] })],
    [
      'POST',
      /^\/api\/invoices\/(\d+)\/approve$/,
      (m, b) =>
        approveInvoice({
          deps,
          invoiceId: m[1],
          user: reviewer,
          note: b.note,
          force: b.force === true,
        }),
    ],
    [
      'POST',
      /^\/api\/invoices\/(\d+)\/reject$/,
      (m, b) => rejectInvoice({ deps, invoiceId: m[1], user: reviewer, reason: b.reason }),
    ],
    [
      'POST',
      /^\/api\/invoices\/(\d+)\/reopen$/,
      (m, b) => reopenInvoice({ deps, invoiceId: m[1], user: reviewer, note: b.note }),
    ],
    [
      'POST',
      /^\/api\/invoices\/(\d+)\/correct$/,
      (m, b) => {
        if (!b.changes || typeof b.changes !== 'object')
          throw new HttpError(400, 'Faltan los cambios');
        return correctInvoice({
          deps,
          invoiceId: m[1],
          user: reviewer,
          note: b.note,
          changes: Object.entries(b.changes),
        });
      },
    ],
    [
      'POST',
      /^\/api\/issues\/(\d+)\/resolve$/,
      (m, b) => resolveIssue({ deps, issueId: m[1], user: reviewer, note: b.note }),
    ],
  ];

  const server = http.createServer(async (req, res) => {
    try {
      const host = req.headers.host ?? '';
      if (host !== `127.0.0.1:${actualPort}` && host !== `localhost:${actualPort}`) {
        throw new HttpError(403, 'Acceso permitido solo desde este equipo');
      }
      const url = new URL(req.url, `http://${host}`);

      if (req.method === 'GET' && url.pathname === '/') {
        const nonce = crypto.randomBytes(16).toString('base64');
        return send(res, 200, renderReviewPage({ token, nonce, reviewer }), {
          'content-type': 'text/html; charset=utf-8',
          'content-security-policy': [
            "default-src 'none'",
            `script-src 'nonce-${nonce}'`,
            `style-src 'nonce-${nonce}'`,
            "connect-src 'self'",
            "frame-src 'self'",
            "img-src 'self' data:",
            "base-uri 'none'",
            "form-action 'none'",
            "frame-ancestors 'none'",
          ].join('; '),
        });
      }

      const documentMatch = /^\/api\/invoices\/(\d+)\/document$/.exec(url.pathname);
      const providedToken =
        req.headers['x-invoicebot-token'] ?? (documentMatch ? url.searchParams.get('token') : null);
      if (!url.pathname.startsWith('/api/')) throw new HttpError(404, 'No encontrado');
      if (!safeEqual(providedToken, token)) throw new HttpError(401, 'Token de sesión no válido');

      if (req.method === 'GET' && documentMatch) {
        const invoice = repository.getInvoiceDetail(Number(documentMatch[1]));
        if (!invoice || !fs.existsSync(invoice.file_path))
          throw new HttpError(404, 'El archivo original ya no existe');
        // Un XML se muestra como texto plano: nunca se interpreta en el navegador.
        const type =
          invoice.mime_type === 'application/pdf' ? 'application/pdf' : 'text/plain; charset=utf-8';
        res.writeHead(200, {
          'content-type': type,
          'content-disposition': 'inline',
          'x-content-type-options': 'nosniff',
          'x-frame-options': 'SAMEORIGIN',
          'cache-control': 'no-store',
        });
        return fs.createReadStream(invoice.file_path).pipe(res);
      }

      for (const [method, pattern, handler] of routes) {
        const match = pattern.exec(url.pathname);
        if (!match || method !== req.method) continue;
        const body = method === 'POST' ? await readJson(req) : null;
        return sendJson(res, 200, await handler(match, body ?? {}, url));
      }
      throw new HttpError(404, 'No encontrado');
    } catch (error) {
      if (error instanceof HttpError) return sendJson(res, error.status, { error: error.message });
      if (error instanceof InvoiceBotError)
        return sendJson(res, 400, { error: error.message, code: error.code });
      logger.error({ err: { message: error.message } }, 'error en la pantalla de revisión');
      return sendJson(res, 500, { error: 'Error interno' });
    }
  });

  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(port, '127.0.0.1', resolve);
  });
  actualPort = server.address().port;
  return {
    url: `http://127.0.0.1:${actualPort}/`,
    port: actualPort,
    token,
    close: () => new Promise((resolve) => server.close(() => resolve())),
  };
}
