/**
 * Puerto DocumentExtractor: obtiene el texto de un documento.
 * Implementación: src/infrastructure/documents/document-router.js.
 *
 * @typedef {object} TextExtraction
 * @property {boolean} needsOcr          true si no hay texto suficiente sin OCR.
 * @property {string | null} text
 * @property {'pdf_text' | 'ocr' | null} method
 * @property {number | null} pageCount
 * @property {number} chars              Caracteres no blancos extraídos.
 *
 * @typedef {object} DocumentExtractor
 * @property {(input: { filePath: string, mimeType: string }) => Promise<TextExtraction>} extractText
 *   Lanza DocumentReadError si el archivo está dañado o cifrado.
 */
export {};
