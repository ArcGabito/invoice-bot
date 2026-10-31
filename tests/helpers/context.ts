import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { loadConfig } from '../../src/shared/config.ts';
import { InvoiceRepository } from '../../src/infrastructure/database/repositories/invoice-repository.ts';
import { UserRepository } from '../../src/infrastructure/database/repositories/user-repository.ts';
import { DocumentRouter } from '../../src/infrastructure/documents/document-router.ts';
import { MockProvider } from '../../src/infrastructure/ai/providers/mock-provider.ts';
import { createLogger } from '../../src/infrastructure/logging/logger.ts';
import type { Deps } from '../../src/application/deps.ts';
import type { AiProvider } from '../../src/application/ports/ai-provider.ts';
import type { Db } from '../../src/infrastructure/database/connection.ts';
import { resetDatabase, workerDatabase, workerDatabaseName } from './db.ts';

export function makeTempDir(prefix = 'invoicebot-test-'): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}

export interface TestContext {
  dir: string;
  db: Db;
  deps: Deps;
}

const noSunat: Deps['sunatClient'] = {
  validateReceipt: () => Promise.reject(new Error('SUNAT no está configurado en esta prueba')),
  ping: () => Promise.reject(new Error('SUNAT no está configurado en esta prueba')),
};

/**
 * Dependencias reales con la base de SQL Server de este proceso de pruebas,
 * vaciada antes de devolverla, y un directorio temporal para los archivos.
 */
export async function makeTestDeps({
  aiProvider = new MockProvider(),
  env = {},
}: { aiProvider?: AiProvider; env?: Record<string, string> } = {}): Promise<TestContext> {
  const dir = makeTempDir();
  const config = loadConfig({
    env: {
      INVOICEBOT_AI_PROVIDER: 'mock',
      INVOICEBOT_DATA_DIR: path.join(dir, 'data'),
      INVOICEBOT_EXPORT_DIR: path.join(dir, 'exports'),
      INVOICEBOT_DB_NAME: workerDatabaseName(),
      ...env,
    },
    cwd: dir,
    loadEnvFile: false,
  });
  const db = await workerDatabase();
  await resetDatabase(db);
  return {
    dir,
    db,
    deps: {
      config,
      logger: createLogger({ level: 'silent' }),
      db,
      repository: new InvoiceRepository(db),
      users: new UserRepository(db),
      documentRouter: new DocumentRouter({ minTextChars: config.minTextChars }),
      aiProvider,
      visionExtractor: null,
      sunatClient: noSunat,
      today: '2026-10-28',
    },
  };
}
