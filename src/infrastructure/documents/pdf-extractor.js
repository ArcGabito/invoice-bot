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

function joinTextItems(items) {
  const lines = [];
  let current = [];
  let lastY = null;
  for (const item of items) {
    if (typeof item.str !== 'string') continue;
    const y = item.transform?.[5];
    if (lastY !== null && y !== undefined && Math.abs(y - lastY) > 2) {
      lines.push(current.join(' '));
      current = [];
    }
    if (item.str.trim() !== '') current.push(item.str.trim());
    if (item.hasEOL) {
      lines.push(current.join(' '));
      current = [];
    }
    if (y !== undefined) lastY = y;
  }
  if (current.length) lines.push(current.join(' '));
  return lines.filter((l) => l.trim() !== '').join('\n');
}
