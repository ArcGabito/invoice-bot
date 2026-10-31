import fs from 'node:fs';
import { getDocument } from 'pdfjs-dist/legacy/build/pdf.mjs';
import { DocumentReadError } from '../../shared/errors.js';

/**
 * Extrae el texto nativo de un PDF, página por página, reconstruyendo las
 * líneas a partir de la posición vertical de cada fragmento.
 *
 * @returns {Promise<{ text: string, pageCount: number }>}
 */
export async function extractPdfText(filePath) {
  const data = new Uint8Array(await fs.promises.readFile(filePath));
  const loadingTask = getDocument({
    data,
    useSystemFonts: false,
    isEvalSupported: false,
    verbosity: 0,
  });
  let pdf;
  try {
    pdf = await loadingTask.promise;
  } catch (error) {
    await loadingTask.destroy();
    const reason =
      error?.name === 'PasswordException'
        ? 'El PDF está protegido con contraseña'
        : 'El PDF está dañado o no se puede leer';
    throw new DocumentReadError(`${reason}: ${error.message}`, { cause: error });
  }

  try {
    const pages = [];
    for (let pageNumber = 1; pageNumber <= pdf.numPages; pageNumber++) {
      const page = await pdf.getPage(pageNumber);
      const content = await page.getTextContent();
      pages.push(joinTextItems(content.items));
      page.cleanup();
    }
    return { text: pages.join('\n\n').trim(), pageCount: pdf.numPages };
  } finally {
    await loadingTask.destroy();
  }
}

/**
 * Glifos sin equivalente Unicode. Algunos generadores de PDF (p. ej. las
 * facturas hechas con Stripe, como las de Cloudflare) no declaran el texto de
 * guiones ni paréntesis, y pdf.js los entrega como U+0000: "IN-45946823"
 * llegaba como "IN \u0000 45946823". Entre dos letras o dígitos casi siempre
 * es un guion (números de factura, EIN, rangos de fechas); en cualquier otra
 * posición se descarta, porque no se puede saber qué carácter era.
 */
function resolveUnmappedGlyphs(line) {
  /* eslint-disable no-control-regex -- U+0000 es justo el carácter que hay que tratar */
  return line
    .replace(/(?<=[\p{L}\p{N}])\u0000+(?=[\p{L}\p{N}])/gu, '-')
    .replace(/\u0000/g, ' ')
    .replace(/ {2,}/g, ' ');
  /* eslint-enable no-control-regex */
}

/**
 * Une los fragmentos de texto en líneas. Dentro de una línea, dos fragmentos
 * se separan con un espacio solo si en la página hay un hueco entre ellos:
 * pdf.js corta el texto en trozos que a menudo van pegados ("IN", "-", "4594…").
 */
export function joinTextItems(items) {
  const lines = [];
  let line = '';
  let lastY = null;
  let lastEndX = null;
  let pendingSpace = false;

  const endLine = () => {
    const resolved = resolveUnmappedGlyphs(line).trim();
    if (resolved !== '') lines.push(resolved);
    line = '';
    lastEndX = null;
    pendingSpace = false;
  };

  for (const item of items) {
    if (typeof item.str !== 'string') continue;
    const [scaleX = 0, skewY = 0, , , x, y] = item.transform ?? [];
    if (lastY !== null && y !== undefined && Math.abs(y - lastY) > 2) endLine();
    if (y !== undefined) lastY = y;

    const text = item.str;
    if (text.trim() === '') {
      if (line !== '') pendingSpace = true;
    } else {
      if (line !== '') {
        const fontSize = Math.hypot(scaleX, skewY) || item.height || 10;
        const gap = x !== undefined && lastEndX !== null ? x - lastEndX : null;
        const separated = gap === null || Math.abs(gap) > fontSize * 0.15;
        if (pendingSpace || separated) line += ' ';
      }
      line += pendingSpace || line === '' ? text.trimStart() : text;
      pendingSpace = /\s$/.test(text);
      line = line.trimEnd();
      if (x !== undefined && typeof item.width === 'number') lastEndX = x + item.width;
    }
    if (item.hasEOL) endLine();
  }
  endLine();
  return lines.join('\n');
}
