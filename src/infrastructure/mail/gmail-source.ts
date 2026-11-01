import { apiJson } from './http.ts';
import type { Fetch } from './http.ts';
import { MAX_MESSAGE_BYTES, MailError } from './mail-source.ts';
import type { MailMessageRef, MailSource } from './mail-source.ts';
import type { TokenProvider } from './oauth.ts';

export const GMAIL_API_URL = 'https://gmail.googleapis.com/gmail/v1';
export const GMAIL_SCOPE = 'https://www.googleapis.com/auth/gmail.readonly';
/** Para mover mensajes (poner etiquetas). No permite borrar mensajes. */
export const GMAIL_MODIFY_SCOPE = 'https://www.googleapis.com/auth/gmail.modify';
/** Tope de mensajes que se listan por ciclo (los más antiguos quedan para el siguiente). */
const MAX_LISTED = 5000;

/**
 * Buzón de Gmail o Google Workspace por la API de Gmail, con permiso de solo
 * lectura (gmail.readonly), o gmail.modify si se ordena el buzón con
 * etiquetas. Sirve con el inicio de sesión del usuario o con una cuenta de
 * servicio con delegación de dominio.
 */
export function createGmailSource({
  tokens,
  userId = 'me',
  label = null,
  inboxOnly = false,
  apiUrl = GMAIL_API_URL,
  timeoutMs = 60_000,
  fetchImpl,
}: {
  tokens: TokenProvider;
  /** "me" (el usuario que inició sesión) o la dirección a la que suplanta la cuenta de servicio. */
  userId?: string;
  /** Etiqueta a leer (p. ej. "Facturas"); null: todo menos enviados, borradores, spam y papelera. */
  label?: string | null;
  /** Solo la bandeja de entrada (cuando los procesados se mueven fuera de ella). */
  inboxOnly?: boolean;
  apiUrl?: string;
  timeoutMs?: number;
  fetchImpl?: Fetch;
}): MailSource {
  const base = `${apiUrl}/users/${encodeURIComponent(userId)}`;
  const options = { tokens, fetchImpl, timeoutMs, service: 'Gmail' };
  const labels = new Map<string, string>();

  /** Id de la etiqueta por nombre; la crea si no existe. */
  async function labelId(name: string): Promise<string> {
    const cached = labels.get(name.toLowerCase());
    if (cached) return cached;
    const list = await apiJson<{ labels?: { id: string; name: string }[] }>(
      `${base}/labels`,
      options,
    );
    let found = list.labels?.find((l) => l.name.toLowerCase() === name.toLowerCase());
    found ??= await apiJson<{ id: string; name: string }>(`${base}/labels`, {
      ...options,
      body: { name, labelListVisibility: 'labelShow', messageListVisibility: 'show' },
    });
    labels.set(name.toLowerCase(), found.id);
    return found.id;
  }

  return {
    provider: 'gmail',
    async connect() {
      const profile = await apiJson<{ emailAddress?: string }>(`${base}/profile`, options);
      if (!profile.emailAddress) throw new MailError('Gmail no devolvió la dirección del buzón');
      return profile.emailAddress;
    },

    async listMessages({ since }): Promise<MailMessageRef[]> {
      // Gmail busca por fecha con segundos desde 1970 en "after:".
      const where = label
        ? `label:"${label.replaceAll('"', '')}"`
        : inboxOnly
          ? 'in:inbox'
          : '-in:sent -in:drafts';
      const q = `has:attachment ${where} after:${Math.floor(since.getTime() / 1000)}`;
      const ids: string[] = [];
      let pageToken: string | undefined;
      do {
        const params = new URLSearchParams({ q, maxResults: '500' });
        if (pageToken) params.set('pageToken', pageToken);
        const page = await apiJson<{ messages?: { id: string }[]; nextPageToken?: string }>(
          `${base}/messages?${params}`,
          options,
        );
        ids.push(...(page.messages ?? []).map((m) => m.id));
        pageToken = page.nextPageToken;
      } while (pageToken && ids.length < MAX_LISTED);
      // Gmail lista del más reciente al más antiguo.
      return ids.reverse().map((id) => ({ id, receivedAt: null }));
    },

    async fetchRaw(id) {
      const message = await apiJson<{ raw?: string; sizeEstimate?: number }>(
        `${base}/messages/${encodeURIComponent(id)}?format=raw`,
        options,
      );
      if (!message.raw) throw new MailError(`Gmail no devolvió el contenido del mensaje ${id}`);
      const raw = Buffer.from(message.raw, 'base64url');
      if (raw.length > MAX_MESSAGE_BYTES) throw new MailError(`Mensaje ${id} demasiado grande`);
      return raw;
    },

    async move(id, folder) {
      await apiJson(`${base}/messages/${encodeURIComponent(id)}/modify`, {
        ...options,
        body: { addLabelIds: [await labelId(folder)], removeLabelIds: ['INBOX'] },
      });
    },

    async close() {},
  };
}
