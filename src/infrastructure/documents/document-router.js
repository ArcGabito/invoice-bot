import { extractPdfText } from './pdf-extractor.js';

/**
 * Decide cómo obtener el texto de un documento. Primero intenta el texto
 * nativo del PDF; si no alcanza el mínimo, el documento necesita OCR.
 *
 * Implementa el puerto DocumentExtractor (src/application/ports/document-extractor.js).
 */
export class DocumentRouter {
  constructor({ minTextChars }) {
    this.minTextChars = minTextChars;
  }

  /**
   * @returns {Promise<{ needsOcr: boolean, text: string | null, method: 'pdf_text' | null, pageCount: number | null, chars: number }>}
   */
  async extractText({ filePath, mimeType }) {
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
