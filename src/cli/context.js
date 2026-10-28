import { loadConfig } from '../shared/config.js';
import { openDatabase } from '../infrastructure/database/connection.js';
import { SqliteInvoiceRepository } from '../infrastructure/database/repositories/sqlite-invoice-repository.js';
import { DocumentRouter } from '../infrastructure/documents/document-router.js';
import { createAiProvider } from '../infrastructure/ai/provider-factory.js';
import { createLogger } from '../infrastructure/logging/logger.js';

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
  const logger = createLogger({ level: globalOptions.verbose ? 'info' : config.logLevel });
  let db;
  let repository;
  let aiProvider;

  return {
    config,
    logger,
    get db() {
      db ??= openDatabase(config.dbPath);
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
    documentRouter: new DocumentRouter({ minTextChars: config.minTextChars }),
    get today() {
      return localIsoDate();
    },
    close() {
      db?.close();
    },
  };
}

/** Fecha local (no UTC) en formato AAAA-MM-DD. */
export function localIsoDate(date = new Date()) {
  const pad = (n) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}
