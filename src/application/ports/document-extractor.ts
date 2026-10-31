import type { RawExtraction } from '../../domain/invoice.ts';

export type TextMethod = 'pdf_text' | 'ocr' | 'xml';

/** Solo en XML: datos con la forma de ExtractionSchema; no hace falta IA. */
export type StructuredDocument =
  { kind: 'invoice'; data: RawExtraction } | { kind: 'cdr' } | { kind: 'other'; root: string };

export interface TextExtraction {
  /** true si no hay texto suficiente sin OCR. */
  needsOcr: boolean;
  text: string | null;
  method: TextMethod | null;
  pageCount: number | null;
  /** Caracteres no blancos extraídos (bytes en XML). */
  chars: number;
  /** true si se intentó OCR (aunque no diera texto suficiente). */
  ocrAttempted?: boolean;
  ocr?: { engine: string; confidence: number | null; pagesRead: number };
  structured?: StructuredDocument;
}

/** Motor de OCR local. */
export interface OcrEngine {
  readonly name: string;
  recognize(input: { filePath: string; mimeType: string }): Promise<{
    text: string;
    confidence: number | null;
    pageCount: number | null;
    pagesRead: number;
  }>;
  close?(): Promise<void>;
}

/**
 * Puerto DocumentExtractor: obtiene el texto de un documento o, si es un
 * comprobante electrónico, sus datos estructurados.
 * Implementación: src/infrastructure/documents/document-router.ts.
 */
export interface DocumentExtractor {
  /** Lanza DocumentReadError si el archivo está dañado o cifrado. */
  extractText(input: { filePath: string; mimeType: string }): Promise<TextExtraction>;
  /** ¿Puede leer documentos sin texto? */
  readonly canOcr?: boolean;
  close?(): Promise<void>;
}
