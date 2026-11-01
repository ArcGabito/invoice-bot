import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { MailError } from '../../infrastructure/mail/mail-source.ts';
import type { MailSource } from '../../infrastructure/mail/mail-source.ts';
import { errorMessage } from '../../shared/errors.ts';
import type { Deps } from '../deps.ts';

export interface MailSummary {
  /** Buzón leído: "gmail:ana@empresa.pe". */
  account: string;
  /** Mensajes con adjuntos en la ventana de búsqueda. */
  listed: number;
  /** Mensajes leídos en este ciclo. */
  read: number;
  /** De ellos, con comprobantes guardados. */
  saved: number;
  /** Archivos guardados en la carpeta de entrada. */
  files: number;
  /** Mensajes con adjuntos que no son comprobantes (p. ej. solo un .docx). */
  noAttachments: number;
  failed: number;
  /** Mensajes nuevos que quedan para el siguiente ciclo (por INVOICEBOT_MAIL_MAX_MESSAGES). */
  remaining: number;
  /** Mensajes movidos a la carpeta de procesados o de revisión. */
  moved: number;
  /** Mensajes que no se pudieron mover (se reintenta en el siguiente ciclo). */
  moveFailed: number;
}

const DAY_MS = 24 * 60 * 60 * 1000;

/** "2026-10-15T14:03:09Z" + id → "2026-10/20261015-140309-1a2b3c4d": una carpeta por mensaje. */
export function messageFolder(date: Date, messageId: string): string {
  const iso = date.toISOString();
  const stamp = `${iso.slice(0, 10).replaceAll('-', '')}-${iso.slice(11, 19).replaceAll(':', '')}`;
  const hash = crypto.createHash('sha256').update(messageId).digest('hex').slice(0, 8);
  return path.join(iso.slice(0, 7), `${stamp}-${hash}`);
}

/**
 * Descarga los comprobantes adjuntos de los correos nuevos a `dir` (una
 * subcarpeta de la carpeta de entrada), para que el escaneo los procese. Los
 * adjuntos de un mensaje quedan juntos en su carpeta: así el PDF se vincula
 * con su XML. Cada mensaje se lee una sola vez; los que fallan se reintentan
 * en los ciclos siguientes hasta INVOICEBOT_MAX_ATTEMPTS.
 *
 * Con INVOICEBOT_MAIL_PROCESSED_FOLDER / _REVIEW_FOLDER, cada mensaje leído
 * se mueve a esa carpeta: con comprobantes, a la de procesados; sin ellos, a
 * la de revisión. Si mover falla (p. ej. falta permiso de escritura) el
 * mensaje queda registrado igual y se intenta moverlo en el ciclo siguiente.
 *
 * Si el servidor rechaza las credenciales se detiene con MailError (auth).
 */
export async function fetchMail({
  deps,
  source,
  dir,
  now = new Date(),
}: {
  deps: Pick<Deps, 'repository' | 'logger' | 'config'>;
  source: MailSource;
  dir: string;
  now?: Date;
}): Promise<MailSummary> {
  const { repository, logger, config } = deps;
  // mailparser solo se carga si hay un buzón configurado: no retrasa el arranque de la CLI.
  const { extractMailFiles } = await import('../../infrastructure/mail/attachments.ts');
  const address = await source.connect();
  const account = `${source.provider}:${address.toLowerCase()}`;
  const log = logger.child({ mail: account });
  try {
    const since = new Date(now.getTime() - config.mail.sinceDays * DAY_MS);
    const refs = await source.listMessages({ since });
    const known = await repository.findMailMessages(
      account,
      refs.map((r) => r.id),
    );
    const pending = refs.filter((r) => {
      const seen = known.get(r.id);
      return !seen || (seen.status === 'failed' && seen.attempts < config.maxAttempts);
    });
    const batch = pending.slice(0, config.mail.maxMessages);
    const { processedFolder, reviewFolder } = config.mail;
    const targetFor = (status: string) =>
      status === 'saved' ? processedFolder : status === 'no_attachments' ? reviewFolder : null;
    const summary: MailSummary = {
      account,
      listed: refs.length,
      read: 0,
      saved: 0,
      files: 0,
      noAttachments: 0,
      failed: 0,
      remaining: pending.length - batch.length,
      moved: 0,
      moveFailed: 0,
    };

    /** Mueve el mensaje si corresponde; un fallo no impide seguir con el resto. */
    const moveMessage = async (id: string, recordId: number, status: string) => {
      const target = targetFor(status);
      if (!target) return;
      try {
        await source.move(id, target);
        await repository.setMailMessageMoved(recordId, target);
        summary.moved++;
      } catch (error) {
        summary.moveFailed++;
        log.warn(
          { id, target, err: { message: errorMessage(error) } },
          'no se pudo mover el mensaje',
        );
      }
    };

    // Leídos en ciclos anteriores que aún no se movieron (p. ej. se activó después o faltaba permiso).
    for (const ref of refs) {
      const seen = known.get(ref.id);
      if (seen && !seen.moved_to && seen.status !== 'failed')
        await moveMessage(ref.id, seen.id, seen.status);
    }

    for (const ref of batch) {
      const record = {
        account,
        messageId: ref.id,
        receivedAt: ref.receivedAt,
        sender: null as string | null,
        subject: null as string | null,
      };
      try {
        const mail = await extractMailFiles(await source.fetchRaw(ref.id));
        record.sender = mail.sender?.slice(0, 320) ?? null;
        record.subject = mail.subject?.slice(0, 500) ?? null;
        record.receivedAt ??= mail.date;
        if (mail.skipped.length)
          log.info({ id: ref.id, skipped: mail.skipped }, 'adjuntos omitidos');
        summary.read++;
        if (mail.files.length === 0) {
          summary.noAttachments++;
          const id = await repository.recordMailMessage({
            ...record,
            status: 'no_attachments',
            files: 0,
            folder: null,
            errorMessage: null,
          });
          await moveMessage(ref.id, id, 'no_attachments');
          continue;
        }
        const folder = messageFolder(new Date(record.receivedAt ?? now), ref.id);
        const target = path.join(dir, folder);
        fs.mkdirSync(target, { recursive: true });
        for (const file of mail.files) fs.writeFileSync(path.join(target, file.name), file.content);
        const id = await repository.recordMailMessage({
          ...record,
          status: 'saved',
          files: mail.files.length,
          folder,
          errorMessage: null,
          attachments: mail.files.map((f) => ({
            sha256: crypto.createHash('sha256').update(f.content).digest('hex'),
            fileName: f.name,
          })),
        });
        await moveMessage(ref.id, id, 'saved');
        summary.saved++;
        summary.files += mail.files.length;
        log.info(
          { id: ref.id, from: record.sender, files: mail.files.map((f) => f.name) },
          'comprobantes descargados del correo',
        );
      } catch (error) {
        // Credenciales rechazadas: no tiene sentido seguir con el resto.
        if (error instanceof MailError && error.auth) throw error;
        summary.failed++;
        log.warn(
          { id: ref.id, err: { message: errorMessage(error) } },
          'no se pudo leer el mensaje',
        );
        await repository.recordMailMessage({
          ...record,
          status: 'failed',
          files: 0,
          folder: null,
          errorMessage: errorMessage(error),
        });
      }
    }
    return summary;
  } finally {
    await source.close();
  }
}
