import { AnthropicProvider } from '../ai/providers/anthropic-provider.ts';
import { TesseractOcr } from './tesseract-ocr.ts';
import type { Config } from '../../shared/config.ts';

/** Motor de OCR local según INVOICEBOT_OCR_ENGINE, o null si no corresponde. */
export function createOcrEngine(config: Config): TesseractOcr | null {
  if (config.ocr.engine !== 'tesseract') return null;
  return new TesseractOcr({
    languageDir: config.ocr.languageDir,
    languages: config.ocr.languages,
    maxPages: config.ocr.maxPages,
    scale: config.ocr.scale,
  });
}

/**
 * Lectura con visión de Claude para documentos sin texto, solo si se eligió
 * INVOICEBOT_OCR_ENGINE=claude: el documento sale del equipo hacia Anthropic.
 */
export function createVisionExtractor(config: Config): AnthropicProvider | null {
  if (config.ocr.engine !== 'claude') return null;
  return new AnthropicProvider({ model: config.anthropic.model, effort: config.anthropic.effort });
}
