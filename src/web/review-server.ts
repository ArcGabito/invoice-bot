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
} from '../application/use-cases/review-invoice.ts';
import { getReviewQueue } from '../application/use-cases/review-queue.ts';
import { authenticate } from '../application/use-cases/users.ts';
import { DOCUMENT_TYPES } from '../domain/invoice.ts';
import { CORRECTABLE_FIELDS, REVIEW_STATUS } from '../domain/review.ts';
import { InvoiceBotError } from '../shared/errors.ts';
import { errorMessage } from '../shared/errors.ts';
import { renderLoginPage, renderReviewPage } from './review-page.ts';
import type { Deps } from '../application/deps.ts';
import type { UserRepository } from '../application/ports/invoice-repository.ts';
import type { AddressInfo } from 'node:net';

type Body = Record<string, unknown>;
type Route = [
  method: string,
  pattern: RegExp,
  handler: (match: RegExpExecArray, body: Body, url: URL, who: string) => unknown,
];

export interface ReviewServerAuth {
  users: UserRepository;
  sessionHours: number;
}

export interface ReviewServer {
  url: string;
  port: number;
  token: string;
  close(): Promise<void>;
}

const MAX_BODY_BYTES = 64 * 1024;
const LOOPBACK = new Set(['127.0.0.1', 'localhost', '::1']);
/** Intentos fallidos antes de bloquear durante LOCK_MS a ese usuario y a esa IP. */
const MAX_FAILED_LOGINS = 5;
const LOCK_MS = 15 * 60 * 1000;

export const isLoopback = (host: string): boolean => LOOPBACK.has(String(host).toLowerCase());

class HttpError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

function send(
  res: http.ServerResponse,
  status: number,
  body: string,
  headers: Record<string, string> = {},
): void {
  res.writeHead(status, {
    'x-content-type-options': 'nosniff',
    'referrer-policy': 'no-referrer',
    'cache-control': 'no-store',
    ...headers,
  });
  res.end(body);
}

const sendJson = (
  res: http.ServerResponse,
  status: number,
  data: unknown,
  headers: Record<string, string> = {},
) =>
  send(res, status, JSON.stringify(data), {
    'content-type': 'application/json; charset=utf-8',
    ...headers,
  });

async function readJson(req: http.IncomingMessage): Promise<Body> {
  if (!/^application\/json\b/.test(req.headers['content-type'] ?? '')) {
    throw new HttpError(415, 'Se esperaba JSON');
  }
  let size = 0;
  const chunks: Buffer[] = [];
  for await (const chunk of req as AsyncIterable<Buffer>) {
    size += chunk.length;
    if (size > MAX_BODY_BYTES) throw new HttpError(413, 'Petición demasiado grande');
    chunks.push(chunk);
  }
  try {
    const parsed: unknown = chunks.length ? JSON.parse(Buffer.concat(chunks).toString('utf8')) : {};
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed))
      throw new Error('no es un objeto');
    return parsed as Body;
  } catch {
    throw new HttpError(400, 'JSON mal formado');
  }
}

/** Texto opcional del cuerpo JSON. */
const text = (value: unknown): string | null => (typeof value === 'string' ? value : null);

const safeEqual = (a: unknown, b: string): boolean =>
  typeof a === 'string' &&
  a.length === b.length &&
  crypto.timingSafeEqual(Buffer.from(a), Buffer.from(b));

function parseCookies(header = ''): Record<string, string> {
  const cookies: Record<string, string> = {};
  for (const part of header.split(';')) {
    const i = part.indexOf('=');
    if (i > 0) cookies[part.slice(0, i).trim()] = part.slice(i + 1).trim();
  }
  return cookies;
}

const pageHeaders = (nonce: string) => ({
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
 * Intentos fallidos de entrada por usuario y por IP. Viven en memoria: tras
 * MAX_FAILED_LOGINS fallos se bloquea esa clave durante LOCK_MS.
 */
class LoginThrottle {
  private readonly failures = new Map<string, { count: number; until: number }>();

  isLocked(keys: string[]): boolean {
    this.purge();
    return keys.some((k) => (this.failures.get(k)?.until ?? 0) > Date.now());
  }

  recordFailure(keys: string[]): void {
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

  clearFailures(keys: string[]): void {
    for (const key of keys) this.failures.delete(key);
  }

  private purge(): void {
    const now = Date.now();
    for (const [key, f] of this.failures) if (f.until && f.until < now) this.failures.delete(key);
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
 *   con su usuario. Con `tls` sirve HTTPS. Las sesiones se guardan en SQL
 *   Server (solo el hash del identificador): reiniciar `serve` no obliga a
 *   entrar de nuevo y varias instancias comparten las sesiones.
 */
export async function startReviewServer({
  deps,
  reviewer = null,
  port = 4317,
  host = '127.0.0.1',
  token = crypto.randomBytes(24).toString('hex'),
  auth = null,
  tls = null,
}: {
  deps: Pick<Deps, 'repository' | 'logger' | 'config' | 'today'>;
  reviewer?: string | null;
  port?: number;
  host?: string;
  token?: string;
  auth?: ReviewServerAuth | null;
  /** cert/key o pfx/passphrase. */
  tls?: https.ServerOptions | null;
}): Promise<ReviewServer> {
  if (!auth && !isLoopback(host)) {
    throw new Error('La pantalla de revisión solo se abre a la red con inicio de sesión');
  }
  const { repository, logger } = deps;
  let actualPort = port;
  const throttle = new LoginThrottle();
  // El prefijo __Host- obliga al navegador a aceptarla solo por HTTPS y para este servidor.
  const cookieName = tls ? '__Host-invoicebot' : 'invoicebot_session';
  const cookieAttributes = `Path=/; HttpOnly; SameSite=Strict${tls ? '; Secure' : ''}`;

  const routes: Route[] = [
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
      async (_m, _body, url) => {
        const view = url.searchParams.get('view') ?? 'pending';
        const summary = await repository.getStatusSummary();
        const counts = {
          pending: (await repository.listInvoicesNeedingReview()).length,
          approved: summary.review.approved ?? 0,
          rejected: summary.review.rejected ?? 0,
          failed: summary.documents.failed ?? 0,
          needsOcr: summary.documents.needs_ocr ?? 0,
        };
        if (view === 'pending') {
          const queue = await getReviewQueue({ deps });
          return {
            view,
            counts,
            items: queue.invoices,
            failed: queue.failed,
            needsOcr: queue.needsOcr,
          };
        }
        if (!(Object.values(REVIEW_STATUS) as string[]).includes(view))
          throw new HttpError(400, 'Vista desconocida');
        return { view, counts, items: await repository.listInvoicesByReviewStatus(view) };
      },
    ],
    ['GET', /^\/api\/invoices\/(\d+)$/, (m) => getInvoiceDetail({ deps, invoiceId: m[1]! })],
    [
      'POST',
      /^\/api\/invoices\/(\d+)\/approve$/,
      (m, b, _url, who) =>
        approveInvoice({
          deps,
          invoiceId: m[1]!,
          user: who,
          note: text(b.note),
          force: b.force === true,
        }),
    ],
    [
      'POST',
      /^\/api\/invoices\/(\d+)\/reject$/,
      (m, b, _url, who) =>
        rejectInvoice({ deps, invoiceId: m[1]!, user: who, reason: text(b.reason) }),
    ],
    [
      'POST',
      /^\/api\/invoices\/(\d+)\/reopen$/,
      (m, b, _url, who) => reopenInvoice({ deps, invoiceId: m[1]!, user: who, note: text(b.note) }),
    ],
    [
      'POST',
      /^\/api\/invoices\/(\d+)\/correct$/,
      (m, b, _url, who) => {
        if (!b.changes || typeof b.changes !== 'object')
          throw new HttpError(400, 'Faltan los cambios');
        return correctInvoice({
          deps,
          invoiceId: m[1]!,
          user: who,
          note: text(b.note),
          changes: Object.entries(b.changes),
        });
      },
    ],
    [
      'POST',
      /^\/api\/issues\/(\d+)\/resolve$/,
      (m, b, _url, who) => resolveIssue({ deps, issueId: m[1]!, user: who, note: text(b.note) }),
    ],
  ];

  /** Entrada con usuario y contraseña (modo en red). */
  async function login(
    req: http.IncomingMessage,
    res: http.ServerResponse,
    users: ReviewServerAuth,
  ) {
    const body = await readJson(req);
    const ip = req.socket.remoteAddress ?? 'desconocida';
    const username = String(body.username ?? '')
      .trim()
      .toLowerCase();
    const keys = [`ip:${ip}`, `user:${username}`];
    if (throttle.isLocked(keys)) {
      logger.warn({ username, ip }, 'acceso bloqueado temporalmente');
      throw new HttpError(429, 'Demasiados intentos fallidos. Espera 15 minutos.');
    }
    const user = await authenticate({ users: users.users, username, password: body.password });
    if (!user) {
      throttle.recordFailure(keys);
      logger.warn({ username, ip }, 'acceso rechazado');
      throw new HttpError(401, 'Usuario o contraseña incorrectos');
    }
    throttle.clearFailures(keys);
    const id = await users.users.createSession(user, { hours: users.sessionHours, ip });
    logger.info({ username: user.username, ip }, 'acceso a la pantalla de revisión');
    return sendJson(
      res,
      200,
      { ok: true },
      {
        'set-cookie': `${cookieName}=${id}; ${cookieAttributes}; Max-Age=${users.sessionHours * 3600}`,
      },
    );
  }

  /**
   * Comprueba el acceso según el modo. Devuelve la URL de la petición y quién
   * firma las acciones, o `null` si ya respondió (páginas, entrada y salida).
   */
  async function guard(
    req: http.IncomingMessage,
    res: http.ServerResponse,
  ): Promise<{ url: URL; who: string } | null> {
    if (!auth) {
      const hostHeader = req.headers.host ?? '';
      if (hostHeader !== `127.0.0.1:${actualPort}` && hostHeader !== `localhost:${actualPort}`) {
        throw new HttpError(403, 'Acceso permitido solo desde este equipo');
      }
      const url = new URL(req.url ?? '/', `http://${hostHeader}`);
      if (req.method === 'GET' && url.pathname === '/') {
        const nonce = crypto.randomBytes(16).toString('base64');
        send(
          res,
          200,
          renderReviewPage({ token, nonce, reviewer: reviewer ?? '' }),
          pageHeaders(nonce),
        );
        return null;
      }
      const documentRequest = /^\/api\/invoices\/\d+\/document$/.test(url.pathname);
      const provided =
        req.headers['x-invoicebot-token'] ??
        (documentRequest ? url.searchParams.get('token') : null);
      if (url.pathname.startsWith('/api/') && !safeEqual(provided, token)) {
        throw new HttpError(401, 'Token de sesión no válido');
      }
      return { url, who: reviewer ?? '' };
    }

    if (tls) res.setHeader('strict-transport-security', 'max-age=31536000');
    const url = new URL(req.url ?? '/', 'http://invoicebot');
    if (req.method === 'POST' && url.pathname === '/login') {
      await login(req, res, auth);
      return null;
    }
    const sessionId = parseCookies(req.headers.cookie)[cookieName];
    const session = await auth.users.getSession(sessionId);
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
      await auth.users.deleteSession(sessionId);
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

  const handler = async (req: http.IncomingMessage, res: http.ServerResponse) => {
    try {
      const access = await guard(req, res);
      if (!access) return;
      const { url, who } = access;
      if (!url.pathname.startsWith('/api/')) throw new HttpError(404, 'No encontrado');

      const documentMatch = /^\/api\/invoices\/(\d+)\/document$/.exec(url.pathname);
      if (req.method === 'GET' && documentMatch) {
        const invoice = await repository.getInvoiceDetail(Number(documentMatch[1]));
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
      logger.error({ err: { message: errorMessage(error) } }, 'error en la pantalla de revisión');
      return sendJson(res, 500, { error: 'Error interno' });
    }
  };

  const server = tls ? https.createServer(tls, handler) : http.createServer(handler);
  await new Promise<void>((resolve, reject) => {
    server.once('error', reject);
    server.listen(port, host, resolve);
  });
  actualPort = (server.address() as AddressInfo).port;
  const shownHost = host === '0.0.0.0' || host === '::' ? 'localhost' : host;
  return {
    url: `${tls ? 'https' : 'http'}://${shownHost.includes(':') ? `[${shownHost}]` : shownHost}:${actualPort}/`,
    port: actualPort,
    token,
    close: () =>
      new Promise<void>((resolve) => {
        server.close(() => resolve());
        server.closeAllConnections();
      }),
  };
}
