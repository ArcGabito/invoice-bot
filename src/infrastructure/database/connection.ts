import { Kysely, MssqlDialect, sql } from 'kysely';
import type {
  KyselyPlugin,
  PluginTransformQueryArgs,
  PluginTransformResultArgs,
  QueryResult,
  RootOperationNode,
  UnknownRow,
} from 'kysely';
import * as tarn from 'tarn';
import * as tedious from 'tedious';
import { ConfigError } from '../../shared/errors.ts';
import type { Config } from '../../shared/config.ts';
import { MIGRATIONS } from './migrations.ts';
import type { Database } from './schema.ts';

export type Db = Kysely<Database>;

export interface DbConnectionOptions {
  server: string;
  port: number;
  database: string;
  user: string;
  password: string;
  encrypt: boolean;
  trustServerCertificate: boolean;
  poolMax?: number;
  /** Nombre visible en sys.dm_exec_sessions (ayuda al DBA a identificar las conexiones). */
  appName?: string;
}

/** Columnas DATE: se devuelven como "AAAA-MM-DD", sin hora. */
const DATE_ONLY_COLUMNS = new Set(['issue_date', 'due_date']);

/**
 * Convierte los valores que devuelve tedious a los tipos que usa la
 * aplicación: fechas a texto ISO (el dominio trabaja con texto) y BIGINT
 * (que tedious entrega como texto para no perder precisión) a número en las
 * columnas de importes *_cents, que nunca superan 2^53.
 */
function normalizeRow(row: UnknownRow): UnknownRow {
  const out: UnknownRow = {};
  for (const [key, value] of Object.entries(row)) {
    if (value instanceof Date) {
      const iso = value.toISOString();
      out[key] = DATE_ONLY_COLUMNS.has(key) ? iso.slice(0, 10) : iso;
    } else if (typeof value === 'string' && key.endsWith('_cents')) {
      out[key] = Number(value);
    } else {
      out[key] = value;
    }
  }
  return out;
}

class NormalizeResultsPlugin implements KyselyPlugin {
  transformQuery(args: PluginTransformQueryArgs): RootOperationNode {
    return args.node;
  }

  async transformResult(args: PluginTransformResultArgs): Promise<QueryResult<UnknownRow>> {
    return { ...args.result, rows: args.result.rows.map(normalizeRow) };
  }
}

/** Crea el grupo de conexiones. No conecta hasta la primera consulta. */
export function createDatabase(options: DbConnectionOptions): Db {
  return new Kysely<Database>({
    dialect: new MssqlDialect({
      tarn: { ...tarn, options: { min: 0, max: options.poolMax ?? 10 } },
      tedious: {
        ...tedious,
        connectionFactory: () =>
          new tedious.Connection({
            server: options.server,
            authentication: {
              type: 'default',
              options: { userName: options.user, password: options.password },
            },
            options: {
              database: options.database,
              port: options.port,
              encrypt: options.encrypt,
              trustServerCertificate: options.trustServerCertificate,
              appName: options.appName ?? 'InvoiceBot',
              useUTC: true,
              connectTimeout: 15_000,
              requestTimeout: 60_000,
            },
          }),
      },
    }),
    plugins: [new NormalizeResultsPlugin()],
  });
}

/** Traduce los errores de conexión de tedious a un mensaje que se pueda resolver. */
export function connectionError(
  error: unknown,
  options: Pick<DbConnectionOptions, 'server' | 'port' | 'database' | 'user'>,
): Error {
  const err = error as { code?: string; message?: string };
  const where = `${options.server}:${options.port}, base de datos ${options.database}, usuario ${options.user}`;
  if (err.code === 'ELOGIN') {
    return new ConfigError(
      `SQL Server rechazó el inicio de sesión (${where}): ${err.message}. Revisa INVOICEBOT_DB_USER e INVOICEBOT_DB_PASSWORD, o crea la base de datos con: invoicebot db init`,
      { cause: error },
    );
  }
  if (err.code === 'ESOCKET' || err.code === 'ETIMEOUT') {
    return new ConfigError(
      `No se pudo conectar con SQL Server (${where}): ${err.message}. Comprueba que el servicio esté iniciado, que acepte TCP en ese puerto y INVOICEBOT_DB_SERVER / INVOICEBOT_DB_PORT.`,
      { cause: error },
    );
  }
  return error instanceof Error ? error : new Error(String(error));
}

export interface MigrationResult {
  applied: string[];
  backup: string | null;
}

/**
 * Aplica las migraciones pendientes, cada una en su transacción. Si la base
 * de datos ya tenía migraciones, antes llama a `backup` (una actualización
 * nunca debe poder perder información).
 */
export async function runMigrations(
  db: Db,
  {
    backup = null,
    upTo = Infinity,
  }: { backup?: ((label: string) => Promise<string>) | null; upTo?: number } = {},
): Promise<MigrationResult> {
  await sql`
    IF OBJECT_ID(N'schema_migrations', N'U') IS NULL
      CREATE TABLE schema_migrations (
        version    INT           NOT NULL CONSTRAINT pk_schema_migrations PRIMARY KEY,
        name       NVARCHAR(100) NOT NULL,
        applied_at DATETIME2(3)  NOT NULL CONSTRAINT df_schema_migrations_applied DEFAULT SYSUTCDATETIME()
      )`.execute(db);
  const applied = new Set(
    (await db.selectFrom('schema_migrations').select('version').execute()).map((r) => r.version),
  );
  const pending = MIGRATIONS.filter((m) => !applied.has(m.version) && m.version <= upTo);
  if (pending.length === 0) return { applied: [], backup: null };

  let backupPath: string | null = null;
  if (backup && applied.size > 0) backupPath = await backup(`v${Math.max(...applied)}`);

  for (const migration of pending) {
    await db.transaction().execute(async (trx) => {
      for (const batch of migration.batches) await sql.raw(batch).execute(trx);
      await trx
        .insertInto('schema_migrations')
        .values({ version: migration.version, name: migration.name })
        .execute();
    });
  }
  return { applied: pending.map((m) => m.name), backup: backupPath };
}

/** Versión del esquema aplicada (0 si la base de datos está vacía). */
export async function schemaVersion(db: Db): Promise<number> {
  const exists = await sql<{
    id: number | null;
  }>`SELECT OBJECT_ID(N'schema_migrations', N'U') AS id`.execute(db);
  if (!exists.rows[0]?.id) return 0;
  const row = await db
    .selectFrom('schema_migrations')
    .select((eb) => eb.fn.max('version').as('version'))
    .executeTakeFirst();
  return row?.version ?? 0;
}

/** "servidor:puerto/base" para mostrar en pantalla (sin usuario ni contraseña). */
export function describeDatabase(config: Pick<Config, 'db'>): string {
  return `${config.db.server}:${config.db.port}/${config.db.name}`;
}

/** Opciones de conexión de la aplicación; la contraseña se lee del entorno, nunca de la configuración. */
export function databaseOptions(
  config: Pick<Config, 'db'>,
  env: Record<string, string | undefined> = process.env,
): DbConnectionOptions {
  const { server, port, name, user, encrypt, trustServerCertificate, poolMax } = config.db;
  const password = env.INVOICEBOT_DB_PASSWORD;
  if (!user || !password) {
    throw new ConfigError(
      'Falta la conexión a SQL Server: define INVOICEBOT_DB_USER e INVOICEBOT_DB_PASSWORD en .env ' +
        '(la base de datos y el usuario se crean con: invoicebot db init)',
    );
  }
  return { server, port, database: name, user, password, encrypt, trustServerCertificate, poolMax };
}

/**
 * Conecta, comprueba que la base de datos responde y aplica las migraciones
 * pendientes. Antes de migrar una base con datos llama a `backup`.
 */
export async function openDatabase(
  options: DbConnectionOptions,
  {
    migrate = true,
    backup = null,
  }: { migrate?: boolean; backup?: ((db: Db, label: string) => Promise<string>) | null } = {},
): Promise<{ db: Db; migration: MigrationResult | null }> {
  const db = createDatabase(options);
  try {
    await sql`SELECT 1 AS ok`.execute(db);
    const migration = migrate
      ? await runMigrations(db, { backup: backup && ((label) => backup(db, label)) })
      : null;
    return { db, migration };
  } catch (error) {
    await db.destroy();
    throw connectionError(error, options);
  }
}
