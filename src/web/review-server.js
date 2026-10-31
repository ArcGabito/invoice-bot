import crypto from 'node:crypto';
import fs from 'node:fs';
import http from 'node:http';
import https from 'node:https';
import {
  approveInvoice,
  correctInvoice,
  getInvoiceDetail,
  rejectInvoice,
  reopenInvoice,
  resolveIssue,
} from '../application/use-cases/review-invoice.js';
import { getReviewQueue } from '../application/use-cases/review-queue.js';
import { authenticate } from '../application/use-cases/users.js';
import { DOCUMENT_TYPES } from '../domain/invoice.js';
import { CORRECTABLE_FIELDS, REVIEW_STATUS } from '../domain/review.js';
import { InvoiceBotError } from '../shared/errors.js';
import { renderLoginPage, renderReviewPage } from './review-page.js';

const MAX_BODY_BYTES = 64 * 1024;
const LOOPBACK = new Set(['127.0.0.1', 'localhost', '::1']);
/** Intentos fallidos antes de bloquear durante LOCK_MS a ese usuario y a esa IP. */
const MAX_FAILED_LOGINS = 5;
const LOCK_MS = 15 * 60 * 1000;

export const isLoopback = (host) => LOOPBACK.has(String(host).toLowerCase());

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

const sendJson = (res, status, data, headers = {}) =>
  send(res, status, JSON.stringify(data), {
    'content-type': 'application/json; charset=utf-8',
    ...headers,
  });

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

function parseCookies(header = '') {
  const cookies = {};
  for (const part of header.split(';')) {
    const i = part.indexOf('=');
    if (i > 0) cookies[part.slice(0, i).trim()] = part.slice(i + 1).trim();
  }
  return cookies;
}

const pageHeaders = (nonce) => ({
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

/**
 * Sesiones de la revisión en red, en memoria: reiniciar `serve` obliga a
 * entrar de nuevo. Una sesión deja de valer al caducar, al desactivar al
 * usuario o al cambiarle la contraseña (ambos cambian su updated_at).
 */
class SessionStore {
  constructor({ users, hours }) {
    this.users = users;
    this.ttl = hours * 60 * 60 * 1000;
    this.sessions = new Map();
    this.failures = new Map();
  }

  create(user) {
    this.purge();
    const id = crypto.randomBytes(32).toString('base64url');
    this.sessions.set(id, {
      userId: user.id,
      userVersion: user.updated_at,
      username: user.username,
      displayName: user.display_name,
      csrf: crypto.randomBytes(24).toString('hex'),
      expiresAt: Date.now() + this.ttl,
    });
    return id;
  }

  get(id) {
    const session = id ? this.sessions.get(id) : null;
    if (!session) return null;
    const user = this.users.getById(session.userId);
    if (
      session.expiresAt < Date.now() ||
      !user?.active ||
      user.updated_at !== session.userVersion
    ) {
      this.sessions.delete(id);
      return null;
    }
    return session;
  }

  delete(id) {
    this.sessions.delete(id);
  }

  purge() {
    const now = Date.now();
    for (const [id, s] of this.sessions) if (s.expiresAt < now) this.sessions.delete(id);
    for (const [key, f] of this.failures) if (f.until && f.until < now) this.failures.delete(key);
  }

  isLocked(keys) {
    return keys.some((k) => (this.failures.get(k)?.until ?? 0) > Date.now());
  }

  recordFailure(keys) {
    for (const key of keys) {
      const f = this.failures.get(key) ?? { count: 0, until: 0 };
      f.count += 1;
      if (f.count >= MAX_FAILED_LOGINS) {
        f.until = Date.now() + LOCK_MS;
        f.count = 0;
      }
      this.failures.set(key, f);
    }
  }

  clearFailures(keys) {
    for (const key of keys) this.failures.delete(key);
  }
}

/**
 * Pantalla de revisión. Dos modos:
 *
 * - **Local** (por defecto, solo 127.0.0.1): exige una cabecera Host local
 *   (evita el DNS rebinding) y el token aleatorio de esta ejecución en cada
 *   llamada a /api (cabecera x-invoicebot-token, o ?token= para ver el
 *   documento original). Las acciones se firman con `reviewer`, la persona
 *   que inició `serve`.
 * - **En red** (con `auth`): cada persona entra con su usuario y contraseña.
 *   La sesión va en una cookie HttpOnly y SameSite=Strict, cada llamada lleva
 *   además el token antifalsificación de su sesión y las acciones se firman
 *   con su usuario. Con `tls` sirve HTTPS.
 *
 * @param {object} options
 * @param {{ users: object, sessionHours: number } | null} [options.auth]
 * @param {import('node:https').ServerOptions | null} [options.tls] cert/key o pfx/passphrase
 * @returns {Promise<{ url: string, port: number, token: string, close: () => Promise<void> }>}
 */
export async function startReviewServer({
  deps,
  reviewer = null,
  port = 4317,
  host = '127.0.0.1',
  token = crypto.randomBytes(24).toString('hex'),
  auth = null,
  tls = null,
}) {
  if (!auth && !isLoopback(host)) {
    throw new Error('La pantalla de revisión solo se abre a la red con inicio de sesión');
  }
  const { repository, logger } = deps;
  let actualPort = port;
  const sessions = auth ? new SessionStore({ users: auth.users, hours: auth.sessionHours }) : null;
  // El prefijo __Host- obliga al navegador a aceptarla solo por HTTPS y para este servidor.
  const cookieName = tls ? '__Host-invoicebot' : 'invoicebot_session';
  const cookieAttributes = `Path=/; HttpOnly; SameSite=Strict${tls ? '; Secure' : ''}`;

  const routes = [
    [
      'GET',
      /^\/api\/meta$/,
      (_m, _b, _url, who) => ({
        reviewer: who,
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
      (m, b, _url, who) =>
        approveInvoice({
          deps,
          invoiceId: m[1],
          user: who,
          note: b.note,
          force: b.force === true,
        }),
    ],
    [
      'POST',
      /^\/api\/invoices\/(\d+)\/reject$/,
      (m, b, _url, who) => rejectInvoice({ deps, invoiceId: m[1], user: who, reason: b.reason }),
    ],
    [
      'POST',
      /^\/api\/invoices\/(\d+)\/reopen$/,
      (m, b, _url, who) => reopenInvoice({ deps, invoiceId: m[1], user: who, note: b.note }),
    ],
    [
      'POST',
      /^\/api\/invoices\/(\d+)\/correct$/,
      (m, b, _url, who) => {
        if (!b.changes || typeof b.changes !== 'object')
          throw new HttpError(400, 'Faltan los cambios');
        return correctInvoice({
          deps,
          invoiceId: m[1],
          user: who,
          note: b.note,
          changes: Object.entries(b.changes),
        });
      },
    ],
    [
      'POST',
      /^\/api\/issues\/(\d+)\/resolve$/,
      (m, b, _url, who) => resolveIssue({ deps, issueId: m[1], user: who, note: b.note }),
    ],
  ];

  /** Entrada con usuario y contraseña (modo en red). */
  async function login(req, res) {
    const body = await readJson(req);
    const ip = req.socket.remoteAddress ?? 'desconocida';
    const username = String(body.username ?? '')
      .trim()
      .toLowerCase();
    const keys = [`ip:${ip}`, `user:${username}`];
    if (sessions.isLocked(keys)) {
      logger.warn({ username, ip }, 'acceso bloqueado temporalmente');
      throw new HttpError(429, 'Demasiados intentos fallidos. Espera 15 minutos.');
    }
    const user = await authenticate({ users: auth.users, username, password: body.password });
    if (!user) {
      sessions.recordFailure(keys);
      logger.warn({ username, ip }, 'acceso rechazado');
      throw new HttpError(401, 'Usuario o contraseña incorrectos');
    }
    sessions.clearFailures(keys);
    const id = sessions.create(user);
    logger.info({ username: user.username, ip }, 'acceso a la pantalla de revisión');
    return sendJson(
      res,
      200,
      { ok: true },
      {
        'set-cookie': `${cookieName}=${id}; ${cookieAttributes}; Max-Age=${auth.sessionHours * 3600}`,
      },
    );
  }

  /**
   * Comprueba el acceso según el modo. Devuelve la URL de la petición y quién
   * firma las acciones, o `null` si ya respondió (páginas, entrada y salida).
   */
  async function guard(req, res) {
    if (!auth) {
      const hostHeader = req.headers.host ?? '';
      if (hostHeader !== `127.0.0.1:${actualPort}` && hostHeader !== `localhost:${actualPort}`) {
        throw new HttpError(403, 'Acceso permitido solo desde este equipo');
      }
      const url = new URL(req.url, `http://${hostHeader}`);
      if (req.method === 'GET' && url.pathname === '/') {
        const nonce = crypto.randomBytes(16).toString('base64');
        send(res, 200, renderReviewPage({ token, nonce, reviewer }), pageHeaders(nonce));
        return null;
      }
      const documentRequest = /^\/api\/invoices\/\d+\/document$/.test(url.pathname);
      const provided =
        req.headers['x-invoicebot-token'] ??
        (documentRequest ? url.searchParams.get('token') : null);
      if (url.pathname.startsWith('/api/') && !safeEqual(provided, token)) {
        throw new HttpError(401, 'Token de sesión no válido');
      }
      return { url, who: reviewer };
    }

    if (tls) res.setHeader('strict-transport-security', 'max-age=31536000');
    const url = new URL(req.url, 'http://invoicebot');
    if (req.method === 'POST' && url.pathname === '/login') {
      await login(req, res);
      return null;
    }
    const sessionId = parseCookies(req.headers.cookie)[cookieName];
    const session = sessions.get(sessionId);
    if (req.method === 'GET' && url.pathname === '/') {
      const nonce = crypto.randomBytes(16).toString('base64');
      const page = session
        ? renderReviewPage({
            token: session.csrf,
            nonce,
            reviewer: session.displayName,
            logout: true,
          })
        : renderLoginPage({ nonce });
      send(res, 200, page, pageHeaders(nonce));
      return null;
    }
    if (!session) throw new HttpError(401, 'Sesión caducada: vuelve a entrar');
    // Token antifalsificación (CSRF): otra web no puede leerlo ni enviarlo.
    const provided = req.headers['x-invoicebot-token'] ?? url.searchParams.get('token');
    if (!safeEqual(provided, session.csrf)) throw new HttpError(403, 'Token de sesión no válido');
    if (req.method === 'POST' && url.pathname === '/logout') {
      sessions.delete(sessionId);
      sendJson(
        res,
        200,
        { ok: true },
        { 'set-cookie': `${cookieName}=; ${cookieAttributes}; Max-Age=0` },
      );
      return null;
    }
    return { url, who: session.username };
  }

  const handler = async (req, res) => {
    try {
      const access = await guard(req, res);
      if (!access) return;
      const { url, who } = access;
      if (!url.pathname.startsWith('/api/')) throw new HttpError(404, 'No encontrado');

      const documentMatch = /^\/api\/invoices\/(\d+)\/document$/.exec(url.pathname);
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

      for (const [method, pattern, route] of routes) {
        const match = pattern.exec(url.pathname);
        if (!match || method !== req.method) continue;
        const body = method === 'POST' ? await readJson(req) : null;
        return sendJson(res, 200, await route(match, body ?? {}, url, who));
      }
      throw new HttpError(404, 'No encontrado');
    } catch (error) {
      if (error instanceof HttpError) return sendJson(res, error.status, { error: error.message });
      if (error instanceof InvoiceBotError)
        return sendJson(res, 400, { error: error.message, code: error.code });
      logger.error({ err: { message: error.message } }, 'error en la pantalla de revisión');
      return sendJson(res, 500, { error: 'Error interno' });
    }
  };

  const server = tls ? https.createServer(tls, handler) : http.createServer(handler);
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(port, host, resolve);
  });
  actualPort = server.address().port;
  const shownHost = host === '0.0.0.0' || host === '::' ? 'localhost' : host;
  return {
    url: `${tls ? 'https' : 'http'}://${shownHost.includes(':') ? `[${shownHost}]` : shownHost}:${actualPort}/`,
    port: actualPort,
    token,
    close: () =>
      new Promise((resolve) => {
        server.close(() => resolve());
        server.closeAllConnections();
      }),
  };
}
