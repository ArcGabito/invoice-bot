import os from 'node:os';
import { loadConfig } from '../shared/config.js';
import { openDatabase } from '../infrastructure/database/connection.js';
import { SqliteInvoiceRepository } from '../infrastructure/database/repositories/sqlite-invoice-repository.js';
import { DocumentRouter } from '../infrastructure/documents/document-router.js';
import { createAiProvider } from '../infrastructure/ai/provider-factory.js';
import { createLogger } from '../infrastructure/logging/logger.js';
import { createSunatClient } from '../infrastructure/sunat/sunat-client.js';
import { createOcrEngine, createVisionExtractor } from '../infrastructure/ocr/ocr-factory.js';

/**
 * Raíz de composición: crea las dependencias reales a partir de la
 * configuración. Cada pieza se crea al pedirla, para que comandos como
 * `config` no abran la base de datos ni necesiten credenciales.
 */
export function createContext(globalOptions = {}) {
  const config = loadConfig({
    overrides: {
      dataDir: globalOptions.dataDir,
      aiProvider: globalOptions.provider,
      concurrency: globalOptions.concurrency,
    },
  });
  const logger = createLogger({
    level: globalOptions.verbose ? 'info' : config.logLevel,
    fileDir: config.log.dir,
    retentionDays: config.log.retentionDays,
  });
  let db;
  let repository;
  let aiProvider;
  let sunatClient;
  let documentRouter;
  let visionExtractor;

  return {
    config,
    logger,
    /** Quién firma las acciones de revisión en el historial. */
    reviewer: globalOptions.user || process.env.INVOICEBOT_USER || os.userInfo().username,
    get db() {
      db ??= openDatabase(config.dbPath, { backupDir: config.backup.dir });
      return db;
    },
    get repository() {
      repository ??= new SqliteInvoiceRepository(this.db);
      return repository;
    },
    get aiProvider() {
      aiProvider ??= createAiProvider(config);
      return aiProvider;
    },
    get sunatClient() {
      sunatClient ??= createSunatClient(config);
      return sunatClient;
    },
    get documentRouter() {
      documentRouter ??= new DocumentRouter({
        minTextChars: config.minTextChars,
        ocrEngine: createOcrEngine(config),
      });
      return documentRouter;
    },
    /** Lectura con visión de Claude (solo con INVOICEBOT_OCR_ENGINE=claude). */
    get visionExtractor() {
      visionExtractor ??= createVisionExtractor(config);
      return visionExtractor;
    },
    get today() {
      return localIsoDate();
    },
    /** Cierra la base de datos y el OCR: un worker de Tesseract abierto impediría terminar. */
    async close() {
      await documentRouter?.close();
      db?.close();
    },
  };
}

/** Fecha local (no UTC) en formato AAAA-MM-DD. */
export function localIsoDate(date = new Date()) {
  const pad = (n) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}
