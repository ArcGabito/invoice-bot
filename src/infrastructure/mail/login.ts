import crypto from 'node:crypto';
import http from 'node:http';
import type { AddressInfo } from 'node:net';
import { GMAIL_SCOPE } from './gmail-source.ts';
import { GRAPH_DELEGATED_SCOPE, MICROSOFT_LOGIN_URL } from './graph-source.ts';
import type { Fetch } from './http.ts';
import { MailError } from './mail-source.ts';
import { requestToken } from './oauth.ts';

export const GOOGLE_AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth';

const LOGIN_TIMEOUT_MS = 5 * 60 * 1000;

const page = (title: string, text: string) =>
  `<!doctype html><html lang="es"><meta charset="utf-8"><title>${title}</title>` +
  `<body style="font-family:system-ui;max-width:32rem;margin:4rem auto"><h1>${title}</h1><p>${text}</p></body></html>`;

/**
 * Inicio de sesión con Google en el navegador (aplicación de escritorio con
 * PKCE): abre la página de Google, recibe la respuesta en un puerto local
 * de este equipo y devuelve el refresh token de solo lectura del correo.
 */
export async function googleLogin({
  clientId,
  clientSecret,
  tokenUrl,
  authUrl = GOOGLE_AUTH_URL,
  scope = GMAIL_SCOPE,
  openBrowser,
  print,
  fetchImpl,
  timeoutMs = LOGIN_TIMEOUT_MS,
}: {
  clientId: string;
  clientSecret: string;
  tokenUrl: string;
  authUrl?: string;
  scope?: string;
  openBrowser: (url: string) => void;
  print: (line: string) => void;
  fetchImpl?: Fetch;
  timeoutMs?: number;
}): Promise<{ refreshToken: string }> {
  const verifier = crypto.randomBytes(32).toString('base64url');
  const challenge = crypto.createHash('sha256').update(verifier).digest('base64url');
  const state = crypto.randomBytes(16).toString('base64url');

  let finish: (result: { code: string } | { error: string }) => void = () => {};
  const received = new Promise<{ code: string } | { error: string }>((resolve) => {
    finish = resolve;
  });
  const server = http.createServer((req, res) => {
    const url = new URL(req.url ?? '/', 'http://127.0.0.1');
    if (url.pathname !== '/') {
      res.writeHead(404).end();
      return;
    }
    const error = url.searchParams.get('error');
    const code = url.searchParams.get('code');
    const ok = !error && code && url.searchParams.get('state') === state;
    res.writeHead(ok ? 200 : 400, { 'content-type': 'text/html; charset=utf-8' });
    res.end(
      ok
        ? page('InvoiceBot', 'Listo: ya puedes cerrar esta pestaña y volver a la consola.')
        : page(
            'InvoiceBot',
            `No se completó el inicio de sesión (${error ?? 'respuesta no válida'}).`,
          ),
    );
    finish(ok ? { code } : { error: error ?? 'respuesta no válida' });
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const redirectUri = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;

  try {
    const url = `${authUrl}?${new URLSearchParams({
      client_id: clientId,
      redirect_uri: redirectUri,
      response_type: 'code',
      scope,
      access_type: 'offline',
      // Pide el consentimiento siempre: Google solo entrega el refresh token la primera vez.
      prompt: 'consent',
      code_challenge: challenge,
      code_challenge_method: 'S256',
      state,
    })}`;
    print('Abre esta dirección en el navegador e inicia sesión con la cuenta de Gmail:');
    print(url);
    openBrowser(url);

    const timer = setTimeout(() => finish({ error: 'tiempo de espera agotado' }), timeoutMs);
    const result = await received.finally(() => clearTimeout(timer));
    if ('error' in result)
      throw new MailError(`Inicio de sesión cancelado: ${result.error}`, { auth: true });

    const body = await requestToken(
      tokenUrl,
      {
        grant_type: 'authorization_code',
        code: result.code,
        client_id: clientId,
        client_secret: clientSecret,
        redirect_uri: redirectUri,
        code_verifier: verifier,
      },
      { fetchImpl },
    );
    if (!body.refresh_token) {
      throw new MailError(
        `Google no entregó el acceso: ${body.error_description ?? body.error ?? `HTTP ${body.status}`}`,
        { auth: true },
      );
    }
    return { refreshToken: body.refresh_token };
  } finally {
    server.close();
  }
}

/**
 * Inicio de sesión con Microsoft por código de dispositivo: muestra un
 * código que se escribe en microsoft.com/devicelogin desde cualquier
 * navegador (sirve también en un servidor sin escritorio).
 */
export async function microsoftDeviceLogin({
  clientId,
  tenant,
  loginUrl = MICROSOFT_LOGIN_URL,
  scope = GRAPH_DELEGATED_SCOPE,
  print,
  fetchImpl = globalThis.fetch,
  sleep = (ms: number) => new Promise((r) => setTimeout(r, ms)),
}: {
  clientId: string;
  tenant: string;
  loginUrl?: string;
  scope?: string;
  print: (line: string) => void;
  fetchImpl?: Fetch;
  sleep?: (ms: number) => Promise<unknown>;
}): Promise<{ refreshToken: string }> {
  const base = `${loginUrl}/${encodeURIComponent(tenant)}/oauth2/v2.0`;
  let res: Response;
  try {
    res = await fetchImpl(`${base}/devicecode`, {
      method: 'POST',
      headers: { 'content-type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({ client_id: clientId, scope }).toString(),
    });
  } catch (error) {
    throw new MailError(`No se pudo contactar a Microsoft: ${(error as Error).message}`);
  }
  const device = (await res.json().catch(() => ({}))) as {
    device_code?: string;
    user_code?: string;
    verification_uri?: string;
    interval?: number;
    expires_in?: number;
    error_description?: string;
  };
  if (!device.device_code || !device.user_code) {
    throw new MailError(
      `Microsoft rechazó el inicio de sesión: ${device.error_description?.split('\r\n')[0] ?? `HTTP ${res.status}`}. ` +
        'Revisa INVOICEBOT_MAIL_MS_CLIENT_ID, INVOICEBOT_MAIL_MS_TENANT y que la aplicación permita "flujos de cliente público".',
      { auth: true },
    );
  }
  print(
    `Abre ${device.verification_uri ?? 'https://microsoft.com/devicelogin'} en un navegador, escribe el código ${device.user_code} e inicia sesión con la cuenta de correo.`,
  );

  let interval = (device.interval ?? 5) * 1000;
  const deadline = Date.now() + (device.expires_in ?? 900) * 1000;
  while (Date.now() < deadline) {
    await sleep(interval);
    const body = await requestToken(
      `${base}/token`,
      {
        grant_type: 'urn:ietf:params:oauth:grant-type:device_code',
        client_id: clientId,
        device_code: device.device_code,
      },
      { fetchImpl },
    );
    if (body.refresh_token) return { refreshToken: body.refresh_token };
    if (body.error === 'authorization_pending') continue;
    if (body.error === 'slow_down') {
      interval += 5000;
      continue;
    }
    throw new MailError(
      `No se completó el inicio de sesión: ${body.error_description?.split('\r\n')[0] ?? body.error ?? `HTTP ${body.status}`}`,
      { auth: true },
    );
  }
  throw new MailError('El código venció sin que se iniciara sesión', { auth: true });
}
