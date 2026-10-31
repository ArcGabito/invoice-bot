import { sql } from 'kysely';
import { dropDatabase } from '../src/infrastructure/database/admin.ts';
import { createDatabase } from '../src/infrastructure/database/connection.ts';
import { TEST_DB_PREFIX, testServerOptions } from './helpers/db.ts';

/**
 * Antes de la ejecución: borra las bases de pruebas de ejecuciones anteriores,
 * para que cada proceso cree la suya con el esquema actual. Sin SQL Server
 * configurado no hace nada (las pruebas que lo necesitan lo explican al fallar).
 */
export default async function setup(): Promise<void> {
  const server = testServerOptions();
  if (!server) {
    if (process.env.INVOICEBOT_TEST_DB_REQUIRED === 'true') {
      throw new Error(
        'INVOICEBOT_TEST_DB_REQUIRED=true pero faltan INVOICEBOT_TEST_DB_USER / _PASSWORD',
      );
    }
    return;
  }
  const master = createDatabase({ ...server, database: 'master', poolMax: 1 });
  try {
    const result = await sql<{ name: string }>`
      SELECT name FROM sys.databases WHERE name LIKE ${`${TEST_DB_PREFIX.replaceAll('_', '[_]')}%`}`.execute(
      master,
    );
    for (const { name } of result.rows) await dropDatabase(server, name);
  } finally {
    await master.destroy();
  }
}
