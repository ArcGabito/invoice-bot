import { ImapFlow } from 'imapflow';
import type { MessageStructureObject } from 'imapflow';
import { errorMessage } from '../../shared/errors.ts';
import { MAX_MESSAGE_BYTES, MailError } from './mail-source.ts';
import type { MailMessageRef, MailSource } from './mail-source.ts';

/** Servidor IMAP por dominio, para no tener que configurarlo en los casos más comunes. */
const KNOWN_HOSTS: Record<string, string> = {
  'gmail.com': 'imap.gmail.com',
  'googlemail.com': 'imap.gmail.com',
  'yahoo.com': 'imap.mail.yahoo.com',
  'zoho.com': 'imap.zoho.com',
};

export const defaultImapHost = (user: string | null): string | null =>
  KNOWN_HOSTS[user?.split('@')[1]?.toLowerCase() ?? ''] ?? null;

/** ¿El mensaje tiene alguna parte que sea un adjunto (y no el cuerpo del correo)? */
export function hasAttachmentPart(node: MessageStructureObject | undefined): boolean {
  if (!node) return false;
  if (node.childNodes?.length) return node.childNodes.some(hasAttachmentPart);
  if (node.disposition?.toLowerCase() === 'attachment') return true;
  const name = node.dispositionParameters?.filename ?? node.parameters?.name;
  return Boolean(name) && !node.type.startsWith('text/');
}

/**
 * Buzón por IMAP (Gmail con contraseña de aplicación, correo de hosting,
 * Zoho, Yahoo…). La carpeta se abre en modo de solo lectura: no se marcan
 * mensajes como leídos.
 */
export function createImapSource({
  host,
  port = 993,
  tls = true,
  user,
  password,
  folder = null,
  writable = false,
  timeoutMs = 60_000,
}: {
  host: string;
  port?: number;
  tls?: boolean;
  user: string;
  password: string;
  folder?: string | null;
  /** Abrir la carpeta con escritura, para mover mensajes. Por defecto, solo lectura. */
  writable?: boolean;
  timeoutMs?: number;
}): MailSource {
  let client: ImapFlow | null = null;
  let uidValidity = '';

  const open = (): ImapFlow => {
    if (!client) throw new MailError('IMAP: no hay conexión abierta');
    return client;
  };

  return {
    provider: 'imap',
    async connect() {
      client = new ImapFlow({
        host,
        port,
        secure: tls,
        auth: { user, pass: password },
        logger: false,
        disableAutoIdle: true,
        connectionTimeout: timeoutMs,
        greetingTimeout: timeoutMs,
        socketTimeout: timeoutMs * 2,
      });
      // Sin este manejador, un corte de red posterior terminaría el proceso.
      client.on('error', () => {});
      try {
        await client.connect();
      } catch (error) {
        client = null;
        const failed = (error as { authenticationFailed?: boolean }).authenticationFailed;
        throw new MailError(
          failed
            ? `El servidor IMAP ${host} rechazó el usuario o la contraseña de ${user}. En Gmail usa una contraseña de aplicación (requiere verificación en dos pasos).`
            : `No se pudo conectar con el servidor IMAP ${host}:${port}: ${errorMessage(error)}`,
          { auth: Boolean(failed), cause: error },
        );
      }
      const name = folder ?? 'INBOX';
      try {
        const mailbox = await client.mailboxOpen(name, { readOnly: !writable });
        uidValidity = String(mailbox.uidValidity);
      } catch (error) {
        throw new MailError(`No se pudo abrir la carpeta "${name}": ${errorMessage(error)}`, {
          cause: error,
        });
      }
      return user;
    },

    async listMessages({ since }): Promise<MailMessageRef[]> {
      const imap = open();
      const uids = (await imap.search({ since }, { uid: true })) || [];
      if (uids.length === 0) return [];
      // IMAP no busca "con adjuntos": se revisa la estructura, sin descargar los mensajes.
      const messages = await imap.fetchAll(
        uids.join(','),
        { uid: true, bodyStructure: true, internalDate: true },
        { uid: true },
      );
      return messages
        .filter((m) => hasAttachmentPart(m.bodyStructure))
        .map((m) => ({
          id: `${uidValidity}:${m.uid}`,
          receivedAt: m.internalDate ? new Date(m.internalDate).toISOString() : null,
        }))
        .sort((a, b) => (a.receivedAt ?? '').localeCompare(b.receivedAt ?? ''));
    },

    async fetchRaw(id) {
      const [validity, uid] = id.split(':');
      if (validity !== uidValidity || !uid) {
        throw new MailError(`El mensaje ${id} ya no existe en la carpeta (UIDVALIDITY cambió)`);
      }
      const message = await open().fetchOne(uid, { source: true, size: true }, { uid: true });
      if (!message || !message.source) throw new MailError(`El mensaje ${id} ya no existe`);
      if (message.source.length > MAX_MESSAGE_BYTES)
        throw new MailError(`Mensaje ${id} demasiado grande`);
      return message.source;
    },

    async move(id, target) {
      const [validity, uid] = id.split(':');
      if (validity !== uidValidity || !uid) throw new MailError(`El mensaje ${id} ya no existe`);
      const imap = open();
      // Crea la carpeta si no existe (si ya existe, el servidor responde con error y se sigue).
      await imap.mailboxCreate(target).catch(() => {});
      const moved = await imap.messageMove(uid, target, { uid: true });
      if (!moved) throw new MailError(`El servidor IMAP no movió el mensaje ${id} a "${target}"`);
    },

    async close() {
      const imap = client;
      client = null;
      await imap?.logout().catch(() => imap.close());
    },
  };
}
