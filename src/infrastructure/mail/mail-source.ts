import { InvoiceBotError } from '../../shared/errors.ts';

/** Mensaje encontrado en el buzón: identificador en el servidor y fecha de recepción. */
export interface MailMessageRef {
  id: string;
  /** ISO 8601, o null si el servidor no la informa. */
  receivedAt: string | null;
}

/**
 * Buzón de correo de solo lectura. Cada conector (IMAP, API de Gmail,
 * Microsoft Graph) solo sabe listar mensajes con adjuntos y descargarlos
 * completos (MIME); leer los adjuntos es común a todos.
 */
export interface MailSource {
  /** Proveedor: imap, gmail o graph. */
  readonly provider: string;
  /** Inicia sesión y devuelve la dirección del buzón. */
  connect(): Promise<string>;
  /** Mensajes con adjuntos recibidos desde `since`, del más antiguo al más reciente. */
  listMessages(options: { since: Date }): Promise<MailMessageRef[]>;
  /** Mensaje completo en formato MIME (RFC 5322). */
  fetchRaw(id: string): Promise<Buffer>;
  /**
   * Mueve el mensaje a una carpeta (Gmail: le pone esa etiqueta y lo saca de
   * la bandeja de entrada), creándola si no existe. Exige permiso de escritura.
   */
  move(id: string, folder: string): Promise<void>;
  close(): Promise<void>;
}

/**
 * Fallo al hablar con el servidor de correo. `auth`: credenciales rechazadas
 * o vencidas (hay que corregir la configuración o volver a iniciar sesión).
 */
export class MailError extends InvoiceBotError {
  auth: boolean;

  constructor(message: string, { auth = false, cause }: { auth?: boolean; cause?: unknown } = {}) {
    super(message, { code: auth ? 'MAIL_AUTH_ERROR' : 'MAIL_ERROR', cause });
    this.auth = auth;
  }
}

/** Tamaño máximo de un mensaje que se descarga (los proveedores limitan a 25-35 MB). */
export const MAX_MESSAGE_BYTES = 50 * 1024 * 1024;
