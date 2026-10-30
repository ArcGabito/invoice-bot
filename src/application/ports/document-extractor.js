/**
 * Puerto DocumentExtractor: obtiene el texto de un documento o, si es un
 * comprobante electrónico, sus datos estructurados.
 * Implementación: src/infrastructure/documents/document-router.js.
 *
 * @typedef {object} TextExtraction
 * @property {boolean} needsOcr          true si no hay texto suficiente sin OCR.
 * @property {string | null} text
 * @property {'pdf_text' | 'ocr' | 'xml' | null} method
 * @property {number | null} pageCount
 * @property {number} chars              Caracteres no blancos extraídos (bytes en XML).
 * @property {{ kind: 'invoice', data: object } | { kind: 'cdr' } | { kind: 'other', root: string }} [structured]
 *   Solo en XML: datos con la forma de ExtractionSchema; no hace falta IA.
 *
 * @typedef {object} DocumentExtractor
 * @property {(input: { filePath: string, mimeType: string }) => Promise<TextExtraction>} extractText
 *   Lanza DocumentReadError si el archivo está dañado o cifrado.
 */
export {};
