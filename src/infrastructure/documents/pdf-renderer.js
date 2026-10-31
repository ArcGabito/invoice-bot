import fs from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import { getDocument } from 'pdfjs-dist/legacy/build/pdf.mjs';
import { DocumentReadError } from '../../shared/errors.js';

const require = createRequire(import.meta.url);
// Fuentes estándar de PDF incluidas en pdfjs-dist: sin ellas, un PDF que mezcla
// texto e imagen se dibujaría con letras sustitutas.
// pdf.js exige que la ruta termine en "/" (también en Windows, donde acepta "/").
const STANDARD_FONTS = `${path
  .join(path.dirname(require.resolve('pdfjs-dist/package.json')), 'standard_fonts')
  .replaceAll('\\', '/')}/`;

/**
 * Dibuja las primeras páginas de un PDF como imágenes PNG, para pasarlas por
 * OCR. Usa el lienzo de @napi-rs/canvas, que pdf.js detecta en Node.
 * `scale` 3 equivale a unos 216 ppp: suficiente para el texto de un comprobante.
 *
 * @returns {Promise<{ images: Buffer[], pageCount: number }>}
 */
export async function renderPdfPages(filePath, { maxPages = 3, scale = 3 } = {}) {
  const data = new Uint8Array(await fs.promises.readFile(filePath));
  const loadingTask = getDocument({
    data,
    isEvalSupported: false,
    verbosity: 0,
    standardFontDataUrl: STANDARD_FONTS,
  });
  let pdf;
  try {
    pdf = await loadingTask.promise;
  } catch (error) {
    await loadingTask.destroy();
    throw new DocumentReadError(`El PDF está dañado o no se puede leer: ${error.message}`, {
      cause: error,
    });
  }

  try {
    const images = [];
    for (let n = 1; n <= Math.min(pdf.numPages, maxPages); n++) {
      const page = await pdf.getPage(n);
      const viewport = page.getViewport({ scale });
      const { canvas, context } = pdf.canvasFactory.create(
        Math.ceil(viewport.width),
        Math.ceil(viewport.height),
      );
      // Fondo blanco: un PDF escaneado puede tener zonas transparentes.
      context.fillStyle = '#ffffff';
      context.fillRect(0, 0, canvas.width, canvas.height);
      await page.render({ canvas, canvasContext: context, viewport }).promise;
      images.push(canvas.toBuffer('image/png'));
      page.cleanup();
    }
    return { images, pageCount: pdf.numPages };
  } finally {
    await loadingTask.destroy();
  }
}
