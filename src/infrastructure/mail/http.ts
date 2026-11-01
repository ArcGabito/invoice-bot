import { errorMessage } from '../../shared/errors.ts';
import { MailError } from './mail-source.ts';
import type { TokenProvider } from './oauth.ts';

export type Fetch = typeof globalThis.fetch;

/** Espera máxima que se respeta de un Retry-After antes de reintentar. */
const MAX_RETRY_WAIT_MS = 10_000;

/**
 * GET autenticado a una API de correo (Gmail o Graph). Reintenta una vez si
 * el servicio pide esperar (429/503) y traduce los errores a MailError.
 */
export async function apiGet(
  url: string,
  {
    tokens,
    fetchImpl = globalThis.fetch,
    timeoutMs,
    service,
  }: { tokens: TokenProvider; fetchImpl?: Fetch; timeoutMs: number; service: string },
): Promise<Response> {
  for (let attempt = 1; ; attempt++) {
    let res: Response;
    try {
      res = await fetchImpl(url, {
        headers: { authorization: `Bearer ${await tokens.getToken()}` },
        signal: AbortSignal.timeout(timeoutMs),
      });
    } catch (error) {
      if (error instanceof MailError) throw error;
      throw new MailError(`No se pudo contactar ${service}: ${errorMessage(error)}`, {
        cause: error,
      });
    }
    if (res.ok) return res;
    if ((res.status === 429 || res.status === 503) && attempt === 1) {
      const seconds = Number(res.headers.get('retry-after'));
      await new Promise((r) =>
        setTimeout(
          r,
          Math.min(Number.isFinite(seconds) ? seconds * 1000 : 2000, MAX_RETRY_WAIT_MS),
        ),
      );
      continue;
    }
    const text = await res.text().catch(() => '');
    let detail = text.slice(0, 300);
    try {
      const body = JSON.parse(text) as { error?: { message?: string } | string };
      detail = typeof body.error === 'string' ? body.error : (body.error?.message ?? detail);
    } catch {
      // respuesta no JSON: se muestra tal cual
    }
    const auth = res.status === 401 || res.status === 403;
    throw new MailError(
      `${service} respondió ${res.status}${detail ? `: ${detail}` : ''}${
        auth ? ' (revisa los permisos de la aplicación y la cuenta)' : ''
      }`,
      { auth },
    );
  }
}

export async function apiJson<T>(url: string, options: Parameters<typeof apiGet>[1]): Promise<T> {
  return (await (await apiGet(url, options)).json()) as T;
}
