import { zipSync } from 'fflate';

export interface MimeAttachment {
  name: string;
  type: string;
  content: Buffer | string;
  /** inline: incrustado en el cuerpo (p. ej. el logo de la firma), con Content-ID. */
  inline?: boolean;
}

/**
 * Correo MIME (multipart/mixed) con adjuntos en base64, como los que envían
 * los sistemas de facturación electrónica.
 */
export function buildMimeMessage({
  from = 'facturacion@proveedor.pe',
  to = 'compras@empresa.pe',
  subject = 'Factura electrónica F001-00001234',
  date = 'Thu, 15 Oct 2026 10:00:00 -0500',
  text = 'Adjuntamos su comprobante electrónico.',
  attachments = [],
}: {
  from?: string;
  to?: string;
  subject?: string;
  date?: string;
  text?: string;
  attachments?: MimeAttachment[];
} = {}): Buffer {
  const boundary = '----=_InvoiceBotTest_0001';
  const wrap = (b64: string) => b64.replace(/.{1,76}/g, '$&\r\n');
  const parts = [
    `Content-Type: text/plain; charset=utf-8\r\nContent-Transfer-Encoding: 8bit\r\n\r\n${text}\r\n`,
    ...attachments.map((a) => {
      const disposition = a.inline ? 'inline' : 'attachment';
      return (
        `Content-Type: ${a.type}; name="${a.name}"\r\n` +
        `Content-Transfer-Encoding: base64\r\n` +
        `Content-Disposition: ${disposition}; filename="${a.name}"\r\n` +
        (a.inline ? `Content-ID: <${a.name}@firma>\r\n` : '') +
        `\r\n${wrap(Buffer.from(a.content).toString('base64'))}`
      );
    }),
  ];
  return Buffer.from(
    `From: Proveedor <${from}>\r\nTo: ${to}\r\nSubject: ${subject}\r\nDate: ${date}\r\n` +
      `Message-ID: <${Math.random().toString(36).slice(2)}@proveedor.pe>\r\nMIME-Version: 1.0\r\n` +
      `Content-Type: multipart/mixed; boundary="${boundary}"\r\n\r\n` +
      parts.map((p) => `--${boundary}\r\n${p}`).join('') +
      `--${boundary}--\r\n`,
  );
}

/** ZIP con los archivos indicados (nombre → contenido). */
export function buildZip(files: Record<string, Buffer | string>): Buffer {
  return Buffer.from(
    zipSync(Object.fromEntries(Object.entries(files).map(([k, v]) => [k, Buffer.from(v)]))),
  );
}
