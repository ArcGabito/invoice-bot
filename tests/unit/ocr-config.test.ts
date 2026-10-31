import path from 'node:path';
import { describe, expect, it } from 'vitest';
import { loadConfig } from '../../src/shared/config.ts';
import {
  createOcrEngine,
  createVisionExtractor,
} from '../../src/infrastructure/ocr/ocr-factory.ts';
import { TesseractOcr } from '../../src/infrastructure/ocr/tesseract-ocr.ts';
import { AnthropicProvider } from '../../src/infrastructure/ai/providers/anthropic-provider.ts';

const config = (env) => loadConfig({ env, loadEnvFile: false });

describe('configuración del OCR', () => {
  it('por defecto usa Tesseract local en español e inglés, dentro del directorio de datos', async () => {
    const cfg = config({ INVOICEBOT_DATA_DIR: 'datos' });
    expect(cfg.ocr).toMatchObject({
      engine: 'tesseract',
      languages: ['spa', 'eng'],
      maxPages: 3,
      scale: 3,
      minConfidence: 70,
    });
    expect(cfg.ocr.languageDir).toBe(path.join(cfg.dataDir, 'ocr'));
    expect(createOcrEngine(cfg)).toBeInstanceOf(TesseractOcr);
    expect(createVisionExtractor(cfg)).toBeNull();
  });

  it('acepta otros idiomas y umbrales', async () => {
    const cfg = config({
      INVOICEBOT_OCR_LANGUAGES: 'spa+por',
      INVOICEBOT_OCR_MAX_PAGES: '5',
      INVOICEBOT_OCR_MIN_CONFIDENCE: '85',
    });
    expect(cfg.ocr).toMatchObject({ languages: ['spa', 'por'], maxPages: 5, minConfidence: 85 });
  });

  it('con visión de Claude no usa Tesseract, y con none no hay OCR', async () => {
    const vision = config({ INVOICEBOT_OCR_ENGINE: 'Claude' });
    expect(createOcrEngine(vision)).toBeNull();
    expect(createVisionExtractor(vision)).toBeInstanceOf(AnthropicProvider);

    const none = config({ INVOICEBOT_OCR_ENGINE: 'none' });
    expect(createOcrEngine(none)).toBeNull();
    expect(createVisionExtractor(none)).toBeNull();
  });

  it('valida los valores', async () => {
    expect(() => config({ INVOICEBOT_OCR_ENGINE: 'abbyy' })).toThrow(/ocr\.engine/);
    expect(() => config({ INVOICEBOT_OCR_LANGUAGES: 'español' })).toThrow(/ocr\.languages/);
    expect(() => config({ INVOICEBOT_OCR_MIN_CONFIDENCE: '120' })).toThrow(/ocr\.minConfidence/);
    expect(() => config({ INVOICEBOT_OCR_MAX_PAGES: '0' })).toThrow(/ocr\.maxPages/);
  });
});
