import type { Extraction } from '../../infrastructure/ai/extraction-schema.ts';

export interface AiUsage {
  input_tokens: number | null;
  output_tokens: number | null;
}

export interface AiExtractionResult {
  /** Datos que cumplen el esquema de extracción. */
  data: Extraction;
  /** Respuesta textual del modelo, para auditoría. */
  raw: string;
  /** Modelo que realmente respondió. */
  model: string;
  usage: AiUsage;
}

/**
 * Puerto AiProvider: cualquier proveedor de IA que extraiga los campos de un
 * comprobante. Implementaciones: src/infrastructure/ai/providers/*.
 */
export interface AiProvider {
  readonly name: string;
  readonly model: string;
  /** Lanza ExtractionError (src/shared/errors.ts) cuando falla. */
  extract(input: { text: string; fileName: string; ocr?: boolean }): Promise<AiExtractionResult>;
  /** Comprueba la conexión sin procesar documentos. */
  ping(): Promise<string>;
}

/** Lectura de un documento adjunto con visión (sin OCR previo). */
export interface VisionExtractor extends AiProvider {
  extractFromFile(input: {
    filePath: string;
    mimeType: string;
    fileName: string;
  }): Promise<AiExtractionResult>;
}
