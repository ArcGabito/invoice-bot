import path from 'node:path';
import { unzipSync } from 'fflate';
import { simpleParser } from 'mailparser';
import type { Attachment } from 'mailparser';

/** Archivos que InvoiceBot sabe leer. Los ZIP se abren y se toman estos de dentro. */
const DOCUMENT_EXTENSIONS = new Set(['.xml', '.pdf', '.png', '.jpg', '.jpeg']);
const IMAGE_EXTENSIONS = new Set(['.png', '.jpg', '.jpeg']);

/** Por tipo MIME, para adjuntos sin nombre. */
const EXTENSION_BY_TYPE: Record<string, string> = {
  'application/pdf': '.pdf',
  'application/xml': '.xml',
  'text/xml': '.xml',
  'image/png': '.png',
  'image/jpeg': '.jpg',
  'application/zip': '.zip',
  'application/x-zip-compressed': '.zip',
};

/**
 * Las imágenes pequeñas suelen ser logos de la firma, no comprobantes: no
 * se envían a OCR ni a la IA.
 */
const MIN_IMAGE_BYTES = 20 * 1024;

/** Límites al abrir un ZIP adjunto (protección contra archivos que se expanden sin fin). */
const ZIP_MAX_ENTRIES = 200;
const ZIP_MAX_FILE_BYTES = 30 * 1024 * 1024;
const ZIP_MAX_TOTAL_BYTES = 100 * 1024 * 1024;

export interface MailFile {
  /** Nombre seguro para guardar en disco. */
  name: string;
  content: Buffer;
}

export interface ParsedMail {
  sender: string | null;
  subject: string | null;
  date: string | null;
  files: MailFile[];
  /** Adjuntos que no se guardaron y por qué (para el registro). */
  skipped: string[];
}

const WINDOWS_RESERVED = /^(con|prn|aux|nul|com\d|lpt\d)$/i;

/**
 * Nombre de archivo seguro: sin rutas (un adjunto llamado "../../x.pdf" no
 * sale de su carpeta), sin caracteres no válidos en Windows y con longitud
 * acotada. Conserva la extensión.
 */
export function safeFileName(name: string, fallback = 'adjunto'): string {
  const base = path.posix.basename(name.replaceAll('\\', '/'));
  // eslint-disable-next-line no-control-regex
  let clean = base.replace(/[<>:"/\\|?*\u0000-\u001f]/g, '_').replace(/^[.\s]+|[.\s]+$/g, '');
  const ext = path.extname(clean).toLowerCase();
  let stem = clean.slice(0, clean.length - ext.length);
  if (!stem || WINDOWS_RESERVED.test(stem)) stem = fallback;
  if (stem.length > 120) stem = stem.slice(0, 120);
  clean = `${stem}${ext}`;
  return clean;
}

function extensionOf(name: string | undefined, contentType: string | undefined): string {
  const ext = name ? path.extname(name).toLowerCase() : '';
  if (ext) return ext;
  return EXTENSION_BY_TYPE[(contentType ?? '').toLowerCase()] ?? '';
}

/** Archivos de un ZIP que InvoiceBot sabe leer (sin carpetas ni archivos ocultos de macOS). */
export function unzipDocuments(zip: Buffer, zipName: string, skipped: string[]): MailFile[] {
  let entries = 0;
  let total = 0;
  let tooBig = false;
  let files: Record<string, Uint8Array>;
  try {
    files = unzipSync(zip, {
      filter: (file) => {
        entries++;
        if (file.name.endsWith('/') || file.name.startsWith('__MACOSX/')) return false;
        if (!DOCUMENT_EXTENSIONS.has(path.extname(file.name).toLowerCase())) return false;
        if (entries > ZIP_MAX_ENTRIES || file.originalSize > ZIP_MAX_FILE_BYTES) {
          tooBig = true;
          return false;
        }
        total += file.originalSize;
        if (total > ZIP_MAX_TOTAL_BYTES) {
          tooBig = true;
          return false;
        }
        return true;
      },
    });
  } catch (error) {
    skipped.push(`${zipName}: ZIP dañado o cifrado (${(error as Error).message})`);
    return [];
  }
  if (tooBig) skipped.push(`${zipName}: se omitieron archivos por exceder los límites del ZIP`);
  return Object.entries(files).map(([name, content]) => ({
    name: safeFileName(name),
    content: Buffer.from(content),
  }));
}

function isDocumentAttachment(a: Attachment, ext: string): boolean {
  if (!DOCUMENT_EXTENSIONS.has(ext)) return false;
  if (!IMAGE_EXTENSIONS.has(ext)) return true;
  // Imágenes: solo adjuntos de verdad, no las incrustadas en el cuerpo (logos, firmas).
  return !a.related && a.contentDisposition !== 'inline' && a.size >= MIN_IMAGE_BYTES;
}

/** Evita que dos adjuntos con el mismo nombre se sobrescriban: "factura.pdf", "factura-2.pdf"… */
function uniqueNames(files: MailFile[]): MailFile[] {
  const used = new Set<string>();
  return files.map((f) => {
    const ext = path.extname(f.name);
    const stem = f.name.slice(0, f.name.length - ext.length);
    let name = f.name;
    for (let n = 2; used.has(name.toLowerCase()); n++) name = `${stem}-${n}${ext}`;
    used.add(name.toLowerCase());
    return { ...f, name };
  });
}

/**
 * Lee un mensaje MIME y devuelve los adjuntos que pueden ser comprobantes:
 * XML, PDF, imágenes adjuntas y el contenido de los ZIP (muchos emisores
 * envían el XML y el PDF comprimidos).
 */
export async function extractMailFiles(raw: Buffer): Promise<ParsedMail> {
  const mail = await simpleParser(raw, {
    skipHtmlToText: true,
    skipTextToHtml: true,
    skipImageLinks: true,
  });
  const skipped: string[] = [];
  const files: MailFile[] = [];
  for (const a of mail.attachments) {
    const ext = extensionOf(a.filename, a.contentType);
    const name = a.filename ?? `adjunto${ext}`;
    if (ext === '.zip') {
      files.push(...unzipDocuments(a.content, name, skipped));
    } else if (isDocumentAttachment(a, ext)) {
      files.push({ name: safeFileName(name, 'adjunto'), content: a.content });
    } else if (IMAGE_EXTENSIONS.has(ext)) {
      // Las incrustadas en el cuerpo (logos, firmas) no se anotan.
      if (!a.related && a.contentDisposition !== 'inline') {
        skipped.push(`${name}: imagen demasiado pequeña para ser un comprobante`);
      }
    } else {
      skipped.push(`${name}: tipo de archivo no soportado`);
    }
  }
  const sender = mail.from?.value[0]?.address ?? null;
  return {
    sender,
    subject: mail.subject ?? null,
    date: mail.date ? mail.date.toISOString() : null,
    files: uniqueNames(files),
    skipped,
  };
}
