import fs from 'node:fs';
import { extractPdfText } from './pdf-extractor.js';
import { parseUbl } from './ubl-parser.js';

export const XML_MIME_TYPE = 'application/xml';

const visibleChars = (text) => text.replace(/\s/g, '').length;

/**
 * Decide cómo obtener los datos de un documento:
 * - XML: se lee el comprobante electrónico UBL directamente, sin IA.
 * - PDF: se extrae el texto nativo; si no alcanza el mínimo, se pasa por OCR.
 * - Imagen: se pasa por OCR.
 * Sin motor de OCR configurado, lo que no tiene texto queda como needsOcr.
 *
 * Implementa el puerto DocumentExtractor (src/application/ports/document-extractor.js).
 */
export class DocumentRouter {
  constructor({ minTextChars, ocrEngine = null }) {
    this.minTextChars = minTextChars;
    this.ocrEngine = ocrEngine;
  }

  /** ¿Puede este enrutador leer documentos sin texto? */
  get canOcr() {
    return this.ocrEngine !== null;
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

    let pageCount = null;
    if (mimeType === 'application/pdf') {
      const native = await extractPdfText(filePath);
      pageCount = native.pageCount;
      const chars = visibleChars(native.text);
      if (chars >= this.minTextChars) {
        return { needsOcr: false, text: native.text, method: 'pdf_text', pageCount, chars };
      }
    }

    // Imagen o PDF escaneado.
    if (!this.ocrEngine) return { needsOcr: true, text: null, method: null, pageCount, chars: 0 };
    const ocr = await this.ocrEngine.recognize({ filePath, mimeType });
    const chars = visibleChars(ocr.text);
    return {
      needsOcr: chars < this.minTextChars,
      ocrAttempted: true,
      text: chars < this.minTextChars ? null : ocr.text,
      method: chars < this.minTextChars ? null : 'ocr',
      pageCount: ocr.pageCount,
      chars,
      ocr: { engine: this.ocrEngine.name, confidence: ocr.confidence, pagesRead: ocr.pagesRead },
    };
  }

  async close() {
    await this.ocrEngine?.close?.();
  }
}
