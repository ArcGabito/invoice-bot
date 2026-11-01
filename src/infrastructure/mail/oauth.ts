import crypto from 'node:crypto';
import { errorMessage } from '../../shared/errors.ts';
import { MailError } from './mail-source.ts';

/** Token de acceso vigente para llamar a la API; lo renueva cuando está por vencer. */
export interface TokenProvider {
  getToken(): Promise<string>;
}

type Fetch = typeof globalThis.fetch;

interface TokenResponse {
  access_token?: string;
  refresh_token?: string;
  expires_in?: number;
  error?: string;
  error_description?: string;
}

/** POST de formulario a un endpoint de tokens OAuth 2.0. */
export async function requestToken(
  url: string,
  form: Record<string, string>,
  {
    fetchImpl = globalThis.fetch,
    timeoutMs = 30_000,
  }: { fetchImpl?: Fetch; timeoutMs?: number } = {},
): Promise<TokenResponse & { status: number }> {
  let res: Response;
  try {
    res = await fetchImpl(url, {
      method: 'POST',
      headers: { 'content-type': 'application/x-www-form-urlencoded', accept: 'application/json' },
      body: new URLSearchParams(form).toString(),
      signal: AbortSignal.timeout(timeoutMs),
    });
  } catch (error) {
    throw new MailError(
      `No se pudo contactar el servicio de inicio de sesión: ${errorMessage(error)}`,
      {
        cause: error,
      },
    );
  }
  const body = (await res.json().catch(() => ({}))) as TokenResponse;
  return { ...body, status: res.status };
}

/** Error de OAuth legible: invalid_grant significa token vencido o revocado. */
function tokenError(body: TokenResponse & { status: number }, what: string): MailError {
  const detail = body.error_description?.split('\r\n')[0] ?? body.error ?? `HTTP ${body.status}`;
  const auth = body.status === 400 || body.status === 401;
  const hint =
    body.error === 'invalid_grant'
      ? ' El acceso venció o fue revocado: vuelve a iniciar sesión con `invoicebot mail login`.'
      : body.error === 'invalid_client' || body.error === 'unauthorized_client'
        ? ' Revisa el client ID y el client secret.'
        : '';
  return new MailError(`${what}: ${detail}.${hint}`, { auth });
}

/**
 * Cachea el token hasta un minuto antes de que venza y evita pedir dos a la
 * vez si llegan llamadas concurrentes.
 */
function cached(fetchToken: () => Promise<{ token: string; expiresIn: number }>): TokenProvider {
  let current: { token: string; expiresAt: number } | null = null;
  let pending: Promise<string> | null = null;
  return {
    getToken() {
      if (current && Date.now() < current.expiresAt) return Promise.resolve(current.token);
      pending ??= fetchToken()
        .then(({ token, expiresIn }) => {
          current = { token, expiresAt: Date.now() + Math.max(expiresIn - 60, 30) * 1000 };
          return token;
        })
        .finally(() => {
          pending = null;
        });
      return pending;
    },
  };
}

/**
 * Inicio de sesión del usuario (delegado) con refresh token. Microsoft
 * entrega uno nuevo en cada renovación: `onRotate` lo guarda para que el
 * acceso no venza a los 90 días.
 */
export function refreshTokenProvider({
  tokenUrl,
  clientId,
  clientSecret,
  refreshToken,
  scope,
  onRotate,
  fetchImpl,
  timeoutMs,
}: {
  tokenUrl: string;
  clientId: string;
  clientSecret?: string | null;
  refreshToken: string;
  scope?: string;
  onRotate?: (refreshToken: string) => void;
  fetchImpl?: Fetch;
  timeoutMs?: number;
}): TokenProvider {
  let current = refreshToken;
  return cached(async () => {
    const body = await requestToken(
      tokenUrl,
      {
        grant_type: 'refresh_token',
        client_id: clientId,
        refresh_token: current,
        ...(clientSecret && { client_secret: clientSecret }),
        ...(scope && { scope }),
      },
      { fetchImpl, timeoutMs },
    );
    if (!body.access_token) throw tokenError(body, 'No se pudo renovar el acceso al correo');
    if (body.refresh_token && body.refresh_token !== current) {
      current = body.refresh_token;
      onRotate?.(current);
    }
    return { token: body.access_token, expiresIn: body.expires_in ?? 3600 };
  });
}

/** Permiso de aplicación (Microsoft 365): client ID + client secret, sin usuario. */
export function clientCredentialsProvider({
  tokenUrl,
  clientId,
  clientSecret,
  scope,
  fetchImpl,
  timeoutMs,
}: {
  tokenUrl: string;
  clientId: string;
  clientSecret: string;
  scope: string;
  fetchImpl?: Fetch;
  timeoutMs?: number;
}): TokenProvider {
  return cached(async () => {
    const body = await requestToken(
      tokenUrl,
      { grant_type: 'client_credentials', client_id: clientId, client_secret: clientSecret, scope },
      { fetchImpl, timeoutMs },
    );
    if (!body.access_token)
      throw tokenError(body, 'Microsoft rechazó las credenciales de la aplicación');
    return { token: body.access_token, expiresIn: body.expires_in ?? 3600 };
  });
}

export interface GoogleServiceAccountKey {
  client_email: string;
  private_key: string;
  token_uri?: string;
}

const base64url = (data: string | Buffer) => Buffer.from(data).toString('base64url');

/**
 * Cuenta de servicio de Google Workspace con delegación de todo el dominio:
 * firma un JWT (RS256) a nombre del buzón `subject`.
 */
export function googleServiceAccountProvider({
  key,
  subject,
  scope,
  fetchImpl,
  timeoutMs,
}: {
  key: GoogleServiceAccountKey;
  subject: string;
  scope: string;
  fetchImpl?: Fetch;
  timeoutMs?: number;
}): TokenProvider {
  const tokenUrl = key.token_uri ?? 'https://oauth2.googleapis.com/token';
  return cached(async () => {
    const now = Math.floor(Date.now() / 1000);
    const unsigned = `${base64url(JSON.stringify({ alg: 'RS256', typ: 'JWT' }))}.${base64url(
      JSON.stringify({
        iss: key.client_email,
        sub: subject,
        scope,
        aud: tokenUrl,
        iat: now,
        exp: now + 3600,
      }),
    )}`;
    let signature: Buffer;
    try {
      signature = crypto.sign('RSA-SHA256', Buffer.from(unsigned), key.private_key);
    } catch (error) {
      throw new MailError(
        `La clave de la cuenta de servicio no es válida: ${errorMessage(error)}`,
        {
          auth: true,
        },
      );
    }
    const body = await requestToken(
      tokenUrl,
      {
        grant_type: 'urn:ietf:params:oauth:grant-type:jwt-bearer',
        assertion: `${unsigned}.${base64url(signature)}`,
      },
      { fetchImpl, timeoutMs },
    );
    if (!body.access_token) {
      throw tokenError(
        body,
        'Google rechazó la cuenta de servicio (¿tiene delegación de dominio con el permiso gmail.readonly?)',
      );
    }
    return { token: body.access_token, expiresIn: body.expires_in ?? 3600 };
  });
}
