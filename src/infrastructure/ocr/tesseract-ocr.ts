import fs from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import Tesseract from 'tesseract.js';
import { ConfigError } from '../../shared/errors.ts';
import { renderPdfPages } from '../documents/pdf-renderer.ts';
import type { OcrEngine } from '../../application/ports/document-extractor.ts';

const require = createRequire(import.meta.url);

/** Motor LSTM de Tesseract: el más preciso y el único incluido en los datos best_int. */
const OEM_LSTM_ONLY = 1;

/** Archivo de datos de un idioma dentro del paquete @tesseract.js-data/<código>. */
function bundledLanguageFile(code: string): string {
  let base: string;
  try {
    base = path.dirname(require.resolve(`@tesseract.js-data/${code}/package.json`));
  } catch {
    throw new ConfigError(
      `Faltan los datos de OCR del idioma "${code}". Instálalos con: npm install @tesseract.js-data/${code}`,
    );
  }
  for (const dir of ['4.0.0_best_int', '4.0.0']) {
    const file = path.join(base, dir, `${code}.traineddata.gz`);
    if (fs.existsSync(file)) return file;
  }
  throw new ConfigError(`El paquete @tesseract.js-data/${code} no trae datos de idioma`);
}

/**
 * Reúne los datos de idioma instalados con InvoiceBot en una sola carpeta,
 * porque Tesseract los lee todos de la misma ruta. Así nunca los descarga de
 * internet. (Pasarlos en memoria no sirve: tesseract.js 7 tiene un error al
 * inicializar con idiomas en memoria.)
 */
export function prepareLanguageDir(dir: string, languages: readonly string[]): string {
  fs.mkdirSync(dir, { recursive: true });
  for (const code of languages) {
    const source = bundledLanguageFile(code);
    const target = path.join(dir, `${code}.traineddata.gz`);
    if (!fs.existsSync(target) || fs.statSync(target).size !== fs.statSync(source).size) {
      fs.copyFileSync(source, target);
    }
  }
  return dir;
}

/**
 * OCR local con Tesseract (WebAssembly, sin servicios externos). Lee fotos y
 * PDF escaneados; un PDF se dibuja primero página por página.
 * Implementa el puerto OcrEngine (src/application/ports/document-extractor.ts).
 */
export class TesseractOcr implements OcrEngine {
  readonly name = 'tesseract';
  readonly languageDir: string;
  readonly languages: string[];
  readonly maxPages: number;
  readonly scale: number;
  private worker: Promise<Tesseract.Worker> | null;

  constructor({
    languageDir,
    languages = ['spa', 'eng'],
    maxPages = 3,
    scale = 3,
  }: {
    languageDir: string;
    languages?: string[];
    maxPages?: number;
    scale?: number;
  }) {
    this.languageDir = languageDir;
    this.languages = languages;
    this.maxPages = maxPages;
    this.scale = scale;
    this.worker = null;
  }

  /** Un solo worker por proceso: crearlo cuesta unos segundos. */
  getWorker(): Promise<Tesseract.Worker> {
    this.worker ??= Promise.resolve().then(() =>
      Tesseract.createWorker(this.languages, OEM_LSTM_ONLY, {
        langPath: prepareLanguageDir(this.languageDir, this.languages),
        gzip: true,
        // Sin caché: no escribe copias de los datos en la carpeta de trabajo.
        cacheMethod: 'none',
      }),
    );
    return this.worker;
  }

  /** confidence: media de 0 a 100 ponderada por la cantidad de texto de cada página. */
  async recognize({ filePath, mimeType }: { filePath: string; mimeType: string }): Promise<{
    text: string;
    confidence: number;
    pageCount: number;
    pagesRead: number;
  }> {
    const { images, pageCount } =
      mimeType === 'application/pdf'
        ? await renderPdfPages(filePath, { maxPages: this.maxPages, scale: this.scale })
        : { images: [await fs.promises.readFile(filePath)], pageCount: 1 };

    const worker = await this.getWorker();
    const pages: { text: string; confidence: number }[] = [];
    for (const image of images) {
      // rotateAuto endereza fotos e imágenes escaneadas torcidas antes de leerlas.
      const { data } = await worker.recognize(image, { rotateAuto: true });
      pages.push({ text: data.text.trim(), confidence: data.confidence });
    }
    const weight = (p: { text: string }) => Math.max(p.text.replace(/\s/g, '').length, 1);
    const totalWeight = pages.reduce((acc, p) => acc + weight(p), 0);
    const confidence =
      pages.reduce((acc, p) => acc + p.confidence * weight(p), 0) / (totalWeight || 1);
    return {
      text: pages
        .map((p) => p.text)
        .join('\n\n')
        .trim(),
      confidence: Math.round(confidence),
      pageCount,
      pagesRead: pages.length,
    };
  }

  async ping(): Promise<string> {
    await this.getWorker();
    return `Tesseract listo (idiomas: ${this.languages.join(', ')}; sin conexión a internet)`;
  }

  async close(): Promise<void> {
    if (!this.worker) return;
    const worker = await this.worker.catch(() => null);
    this.worker = null;
    await worker?.terminate();
  }
}
