import os from 'node:os';
import { loadConfig } from '../shared/config.ts';
import type { Config } from '../shared/config.ts';
import { databaseOptions, openDatabase } from '../infrastructure/database/connection.ts';
import type { Db, MigrationResult } from '../infrastructure/database/connection.ts';
import { createBackup } from '../infrastructure/database/backup.ts';
import { InvoiceRepository } from '../infrastructure/database/repositories/invoice-repository.ts';
import { UserRepository } from '../infrastructure/database/repositories/user-repository.ts';
import { DocumentRouter } from '../infrastructure/documents/document-router.ts';
import { createAiProvider } from '../infrastructure/ai/provider-factory.ts';
import { createLogger } from '../infrastructure/logging/logger.ts';
import { createSunatClient } from '../infrastructure/sunat/sunat-client.ts';
import type { SunatClient } from '../infrastructure/sunat/sunat-client.ts';
import { createOcrEngine, createVisionExtractor } from '../infrastructure/ocr/ocr-factory.ts';
import { InvoiceBotError } from '../shared/errors.ts';
import type { Deps } from '../application/deps.ts';
import type { AiProvider, VisionExtractor } from '../application/ports/ai-provider.ts';

export interface GlobalOptions {
  dataDir?: string;
  provider?: string;
  concurrency?: number;
  json?: boolean;
  verbose?: boolean;
  user?: string;
}

export interface Context extends Deps {
  /** Quién firma las acciones de revisión en el historial. */
  reviewer: string;
  /** Conecta con SQL Server y aplica las migraciones pendientes. */
  connect(): Promise<MigrationResult | null>;
  /** Cierra la base de datos y el OCR: un worker de Tesseract abierto impediría terminar. */
  close(): Promise<void>;
}

/**
 * Raíz de composición: crea las dependencias reales a partir de la
 * configuración. Cada pieza se crea al pedirla, para que comandos como
 * `config` no abran la base de datos ni necesiten credenciales. La base de
 * datos se abre con `connect()` (la CLI lo hace antes de cada comando que la usa).
 */
export function createContext(globalOptions: GlobalOptions = {}): Context {
  const config: Readonly<Config> = loadConfig({
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
  let db: Db | undefined;
  let repository: InvoiceRepository | undefined;
  let users: UserRepository | undefined;
  let aiProvider: AiProvider | undefined;
  let sunatClient: SunatClient | undefined;
  let documentRouter: DocumentRouter | undefined;
  let visionExtractor: VisionExtractor | null | undefined;

  const requireDb = (): Db => {
    if (!db) throw new InvoiceBotError('La base de datos no está abierta (falta connect())');
    return db;
  };

  return {
    config,
    logger,
    reviewer: globalOptions.user || process.env.INVOICEBOT_USER || os.userInfo().username,
    async connect() {
      if (db) return null;
      const opened = await openDatabase(databaseOptions(config), {
        // Antes de migrar una base con datos, copia de seguridad en la carpeta configurada.
        backup: async (opening, label) =>
          (await createBackup(opening, config.backup.dir, { label })).path,
      });
      db = opened.db;
      if (opened.migration?.applied.length) {
        logger.info(
          { applied: opened.migration.applied, backup: opened.migration.backup },
          'migraciones aplicadas',
        );
      }
      return opened.migration;
    },
    get db() {
      return requireDb();
    },
    get repository() {
      repository ??= new InvoiceRepository(requireDb());
      return repository;
    },
    /** Usuarios de la pantalla de revisión en red. */
    get users() {
      users ??= new UserRepository(requireDb());
      return users;
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
      if (visionExtractor === undefined) visionExtractor = createVisionExtractor(config);
      return visionExtractor;
    },
    get today() {
      return localIsoDate();
    },
    async close() {
      await documentRouter?.close();
      await db?.destroy();
      db = undefined;
    },
  };
}

/** Fecha local (no UTC) en formato AAAA-MM-DD. */
export function localIsoDate(date = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}
