import fs from 'node:fs';
import { extractPdfText } from './pdf-extractor.js';
import { parseUbl } from './ubl-parser.js';

export const XML_MIME_TYPE = 'application/xml';

/**
 * Decide cómo obtener los datos de un documento:
 * - XML: se lee el comprobante electrónico UBL directamente, sin IA.
 * - PDF: se extrae el texto nativo; si no alcanza el mínimo, necesita OCR.
 * - Imagen: necesita OCR.
 *
 * Implementa el puerto DocumentExtractor (src/application/ports/document-extractor.js).
 */
export class DocumentRouter {
  constructor({ minTextChars }) {
    this.minTextChars = minTextChars;
  }

  async extractText({ filePath, mimeType }) {
    if (mimeType === XML_MIME_TYPE) {
      const buffer = await fs.promises.readFile(filePath);
      return {
        needsOcr: false,
        text: null,
        method: 'xml',
        pageCount: null,
        chars: buffer.length,
        structured: parseUbl(buffer),
      };
    }
    if (mimeType !== 'application/pdf') {
      // Las imágenes siempre requieren OCR (disponible a partir de v0.2.0).
      return { needsOcr: true, text: null, method: null, pageCount: null, chars: 0 };
    }
    const { text, pageCount } = await extractPdfText(filePath);
    const chars = text.replace(/\s/g, '').length;
    if (chars < this.minTextChars) {
      return { needsOcr: true, text: null, method: null, pageCount, chars };
    }
    return { needsOcr: false, text, method: 'pdf_text', pageCount, chars };
  }
}
