import { apiGet, apiJson } from './http.ts';
import type { Fetch } from './http.ts';
import { MAX_MESSAGE_BYTES, MailError } from './mail-source.ts';
import type { MailMessageRef, MailSource } from './mail-source.ts';
import type { TokenProvider } from './oauth.ts';

export const GRAPH_API_URL = 'https://graph.microsoft.com/v1.0';
export const MICROSOFT_LOGIN_URL = 'https://login.microsoftonline.com';
/** Permiso delegado (inicio de sesión del usuario): leer su correo y renovar el acceso. */
export const GRAPH_DELEGATED_SCOPE = 'offline_access https://graph.microsoft.com/Mail.Read';
/** Permiso de aplicación: el que otorga el administrador de Microsoft 365 (Mail.Read). */
export const GRAPH_APP_SCOPE = 'https://graph.microsoft.com/.default';
const MAX_LISTED = 5000;

/** Carpetas con nombre fijo en Graph (no dependen del idioma del buzón). */
const WELL_KNOWN_FOLDERS = new Set(['inbox', 'archive', 'junkemail', 'sentitems', 'deleteditems']);

export const microsoftTokenUrl = (tenant: string, loginUrl = MICROSOFT_LOGIN_URL) =>
  `${loginUrl}/${encodeURIComponent(tenant)}/oauth2/v2.0/token`;

/**
 * Buzón de Microsoft 365 u Outlook.com por Microsoft Graph, con permiso de
 * solo lectura (Mail.Read). `mailbox` null: el del usuario que inició sesión
 * (/me); con dirección: ese buzón, con permiso de aplicación.
 */
export function createGraphSource({
  tokens,
  mailbox = null,
  folder = null,
  apiUrl = GRAPH_API_URL,
  timeoutMs = 60_000,
  fetchImpl,
}: {
  tokens: TokenProvider;
  mailbox?: string | null;
  /** Carpeta: inbox (por defecto), otra carpeta conocida o el nombre de una carpeta propia. */
  folder?: string | null;
  apiUrl?: string;
  timeoutMs?: number;
  fetchImpl?: Fetch;
}): MailSource {
  const root = mailbox ? `${apiUrl}/users/${encodeURIComponent(mailbox)}` : `${apiUrl}/me`;
  const options = { tokens, fetchImpl, timeoutMs, service: 'Microsoft Graph' };
  let folderId: string | null = null;

  /** Id de la carpeta: nombre conocido, o búsqueda por nombre en la raíz y dentro de la bandeja de entrada. */
  async function resolveFolder(): Promise<string> {
    if (folderId) return folderId;
    const name = folder ?? 'inbox';
    if (WELL_KNOWN_FOLDERS.has(name.toLowerCase())) return (folderId = name.toLowerCase());
    const filter = encodeURIComponent(`displayName eq '${name.replaceAll("'", "''")}'`);
    for (const parent of [`${root}/mailFolders`, `${root}/mailFolders/inbox/childFolders`]) {
      const found = await apiJson<{ value: { id: string }[] }>(
        `${parent}?$filter=${filter}&$select=id`,
        options,
      );
      if (found.value[0]) return (folderId = found.value[0].id);
    }
    throw new MailError(`No existe la carpeta "${name}" en el buzón`);
  }

  return {
    provider: 'graph',
    async connect() {
      if (mailbox) {
        // Comprueba que la aplicación tiene acceso a ese buzón.
        await apiGet(`${root}/mailFolders/inbox?$select=id`, options);
        await resolveFolder();
        return mailbox;
      }
      const me = await apiJson<{ mail?: string | null; userPrincipalName?: string }>(
        `${root}?$select=mail,userPrincipalName`,
        options,
      );
      await resolveFolder();
      const address = me.mail ?? me.userPrincipalName;
      if (!address) throw new MailError('Microsoft Graph no devolvió la dirección del buzón');
      return address;
    },

    async listMessages({ since }): Promise<MailMessageRef[]> {
      const params = new URLSearchParams({
        $filter: `hasAttachments eq true and receivedDateTime ge ${since.toISOString()}`,
        $select: 'id,receivedDateTime',
        $top: '100',
      });
      let url: string | undefined =
        `${root}/mailFolders/${encodeURIComponent(await resolveFolder())}/messages?${params}`;
      const refs: MailMessageRef[] = [];
      while (url && refs.length < MAX_LISTED) {
        const page: {
          value: { id: string; receivedDateTime?: string }[];
          '@odata.nextLink'?: string;
        } = await apiJson(url, options);
        refs.push(...page.value.map((m) => ({ id: m.id, receivedAt: m.receivedDateTime ?? null })));
        url = page['@odata.nextLink'];
      }
      return refs.sort((a, b) => (a.receivedAt ?? '').localeCompare(b.receivedAt ?? ''));
    },

    async fetchRaw(id) {
      const res = await apiGet(`${root}/messages/${encodeURIComponent(id)}/$value`, options);
      const raw = Buffer.from(await res.arrayBuffer());
      if (raw.length > MAX_MESSAGE_BYTES) throw new MailError(`Mensaje ${id} demasiado grande`);
      return raw;
    },

    async close() {},
  };
}
