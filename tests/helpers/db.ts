import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { parse } from 'dotenv';
import { CompiledQuery, sql } from 'kysely';
import { createEmptyDatabase } from '../../src/infrastructure/database/admin.ts';
import { createDatabase, runMigrations } from '../../src/infrastructure/database/connection.ts';
import type { Db, DbConnectionOptions } from '../../src/infrastructure/database/connection.ts';

/**
 * Base de datos de las pruebas: un SQL Server real (el de docker-compose, el
 * de CI o uno local preparado con scripts/dev/setup-local-sqlserver.ps1).
 * La conexión se lee de INVOICEBOT_TEST_DB_* (variables de entorno o el
 * archivo .env.test de la raíz). Cada proceso de Vitest usa su propia base
 * invoicebot_test_<n>, que se vacía antes de cada prueba.
 */
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
export const TEST_DB_PREFIX = 'invoicebot_test_';

function testEnv(): Record<string, string | undefined> {
  const file = path.join(ROOT, '.env.test');
  const fromFile = fs.existsSync(file) ? parse(fs.readFileSync(file)) : {};
  return { ...fromFile, ...process.env };
}

/** Servidor y cuenta de las pruebas (con permiso para crear bases de datos), o null si no hay. */
export function testServerOptions(): Omit<DbConnectionOptions, 'database'> | null {
  const env = testEnv();
  const user = env.INVOICEBOT_TEST_DB_USER;
  const password = env.INVOICEBOT_TEST_DB_PASSWORD;
  if (!user || !password) return null;
  return {
    server: env.INVOICEBOT_TEST_DB_SERVER || 'localhost',
    port: Number(env.INVOICEBOT_TEST_DB_PORT || 1433),
    user,
    password,
    encrypt: env.INVOICEBOT_TEST_DB_ENCRYPT !== 'false',
    trustServerCertificate: env.INVOICEBOT_TEST_DB_TRUST_CERT === 'true',
    poolMax: 10,
  };
}

export const hasTestDatabase = (): boolean => testServerOptions() !== null;

export const workerDatabaseName = (): string =>
  `${TEST_DB_PREFIX}${process.env.VITEST_POOL_ID ?? process.env.VITEST_WORKER_ID ?? '0'}`;

export function requireServer(): Omit<DbConnectionOptions, 'database'> {
  const server = testServerOptions();
  if (!server) {
    throw new Error(
      'Las pruebas de integración necesitan SQL Server: define INVOICEBOT_TEST_DB_USER e INVOICEBOT_TEST_DB_PASSWORD ' +
        '(o crea .env.test con scripts/dev/setup-local-sqlserver.ps1, o levanta docker compose).',
    );
  }
  return server;
}

/** Variables INVOICEBOT_DB_* para que un proceso de la CLI use la base de este proceso de pruebas. */
export function workerDatabaseEnv(): Record<string, string> {
  const s = requireServer();
  return {
    INVOICEBOT_DB_SERVER: s.server,
    INVOICEBOT_DB_PORT: String(s.port),
    INVOICEBOT_DB_NAME: workerDatabaseName(),
    INVOICEBOT_DB_USER: s.user,
    INVOICEBOT_DB_PASSWORD: s.password,
    INVOICEBOT_DB_ENCRYPT: String(s.encrypt),
    INVOICEBOT_DB_TRUST_CERT: String(s.trustServerCertificate),
  };
}

let shared: Promise<Db> | null = null;

/** Conexión a la base de este proceso de pruebas; la crea y la migra la primera vez. */
export function workerDatabase(): Promise<Db> {
  shared ??= (async () => {
    const server = requireServer();
    const database = workerDatabaseName();
    const master = createDatabase({ ...server, database: 'master', poolMax: 1 });
    try {
      const exists = await sql<{ id: number | null }>`SELECT DB_ID(${database}) AS id`.execute(
        master,
      );
      if (!exists.rows[0]?.id) await createEmptyDatabase(server, database);
    } finally {
      await master.destroy();
    }
    const db = createDatabase({ ...server, database });
    await runMigrations(db);
    return db;
  })();
  return shared;
}

/** Cierra la conexión compartida (al terminar cada archivo de pruebas). */
export async function closeWorkerDatabase(): Promise<void> {
  const pending = shared;
  shared = null;
  if (pending) await (await pending.catch(() => null))?.destroy();
}

/** Tablas en orden de borrado (las que referencian a otras, primero). */
const TABLES = [
  'review_sessions',
  'users',
  'review_actions',
  'sunat_checks',
  'validation_issues',
  'invoices',
  'extraction_runs',
  'file_occurrences',
  'documents',
  'bot_runs',
] as const;

/**
 * Vacía todas las tablas y reinicia los identificadores, para que cada prueba
 * empiece con la base como recién creada (la primera factura vuelve a ser #1).
 */
export async function resetDatabase(db: Db): Promise<void> {
  await sql
    .raw(`UPDATE documents SET linked_document_id = NULL WHERE linked_document_id IS NOT NULL`)
    .execute(db);
  for (const table of TABLES) await sql.raw(`DELETE FROM ${table}`).execute(db);
  // RESEED 0 solo en tablas que ya tuvieron filas: en una recién creada el siguiente valor sería 0.
  const used = await sql<{ name: string }>`
    SELECT OBJECT_NAME(object_id) AS name FROM sys.identity_columns
    WHERE last_value IS NOT NULL AND OBJECTPROPERTY(object_id, 'IsUserTable') = 1`.execute(db);
  for (const { name } of used.rows) {
    if (name !== 'schema_migrations')
      await sql.raw(`DBCC CHECKIDENT ('${name}', RESEED, 0) WITH NO_INFOMSGS`).execute(db);
  }
}

/**
 * Consultas SQL directas para comprobar el estado de la base en las pruebas.
 * Los parámetros se escriben con `?` y se pasan en orden.
 */
async function rawQuery<T>(db: Db, text: string, params: unknown[]): Promise<T[]> {
  let n = 0;
  const compiled = CompiledQuery.raw(
    text.replace(/\?/g, () => `@${++n}`),
    params,
  );
  return (await db.executeQuery<T>(compiled)).rows;
}

type Row = Record<string, any>;

export const sqlAll = <T = Row>(db: Db, text: string, ...params: unknown[]) =>
  rawQuery<T>(db, text, params);

/** Primera fila; en las pruebas se da por hecho que existe (si no, la aserción siguiente falla). */
export const sqlGet = async <T = Row>(db: Db, text: string, ...params: unknown[]): Promise<T> =>
  (await rawQuery<T>(db, text, params))[0] as T;

/** Primer valor de la primera fila (p. ej. un COUNT). */
export const sqlValue = async <T = unknown>(
  db: Db,
  text: string,
  ...params: unknown[]
): Promise<T> => {
  const row = (await rawQuery<Row>(db, text, params))[0];
  return (row ? Object.values(row)[0] : undefined) as T;
};

export const sqlRun = async (db: Db, text: string, ...params: unknown[]): Promise<void> => {
  await rawQuery(db, text, params);
};

export const countRows = (db: Db, table: string): Promise<number> =>
  sqlValue<number>(db, `SELECT COUNT(*) AS n FROM ${table}`);

/** Primera columna de todas las filas (p. ej. los códigos de las incidencias). */
export const sqlColumn = async <T = unknown>(
  db: Db,
  text: string,
  ...params: unknown[]
): Promise<T[]> => (await rawQuery<Row>(db, text, params)).map((row) => Object.values(row)[0] as T);
