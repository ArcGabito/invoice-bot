import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { loadConfig } from '../../src/shared/config.js';
import { openDatabase } from '../../src/infrastructure/database/connection.js';
import { SqliteInvoiceRepository } from '../../src/infrastructure/database/repositories/sqlite-invoice-repository.js';
import { DocumentRouter } from '../../src/infrastructure/documents/document-router.js';
import { MockProvider } from '../../src/infrastructure/ai/providers/mock-provider.js';
import { createLogger } from '../../src/infrastructure/logging/logger.js';

export function makeTempDir(prefix = 'invoicebot-test-') {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}

/** Dependencias reales con base de datos en un directorio temporal. */
export function makeTestDeps({ aiProvider = new MockProvider(), env = {} } = {}) {
  const dir = makeTempDir();
  const config = loadConfig({
    env: {
      INVOICEBOT_AI_PROVIDER: 'mock',
      INVOICEBOT_DATA_DIR: path.join(dir, 'data'),
      INVOICEBOT_EXPORT_DIR: path.join(dir, 'exports'),
      ...env,
    },
    cwd: dir,
    loadEnvFile: false,
  });
  const db = openDatabase(config.dbPath);
  return {
    dir,
    db,
    deps: {
      config,
      logger: createLogger({ level: 'silent' }),
      repository: new SqliteInvoiceRepository(db),
      documentRouter: new DocumentRouter({ minTextChars: config.minTextChars }),
      aiProvider,
      today: '2026-10-28',
    },
  };
}
