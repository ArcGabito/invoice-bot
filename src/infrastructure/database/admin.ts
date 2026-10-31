import { sql } from 'kysely';
import { ConfigError } from '../../shared/errors.ts';
import { connectionError, createDatabase } from './connection.ts';
import type { DbConnectionOptions } from './connection.ts';

/** Nombres de base de datos e inicio de sesión: se insertan en DDL, así que solo se admite un formato seguro. */
const IDENTIFIER = /^[A-Za-z][A-Za-z0-9_]{0,63}$/;

export function checkIdentifier(value: string, what: string): string {
  if (!IDENTIFIER.test(value)) {
    throw new ConfigError(
      `${what} "${value}" no es válido: usa letras, números y guion bajo, empezando por una letra`,
    );
  }
  return value;
}

/** Comillas de cadena T-SQL para el DDL que no admite parámetros (CREATE LOGIN … PASSWORD). */
const quoteString = (value: string) => `N'${value.replace(/'/g, "''")}'`;

export interface InitDatabaseResult {
  databaseCreated: boolean;
  loginCreated: boolean;
  userCreated: boolean;
}

/**
 * Crea la base de datos de InvoiceBot y el inicio de sesión de la aplicación
 * (si no existen) con una cuenta de administrador. La aplicación queda como
 * db_owner solo de su base de datos: puede aplicar migraciones y hacer copias
 * de seguridad, pero no ve nada más del servidor.
 */
export async function initDatabase({
  admin,
  database,
  appLogin,
  appPassword,
}: {
  admin: Omit<DbConnectionOptions, 'database'>;
  database: string;
  appLogin: string;
  appPassword: string;
}): Promise<InitDatabaseResult> {
  checkIdentifier(database, 'El nombre de la base de datos');
  checkIdentifier(appLogin, 'El inicio de sesión');
  const master = createDatabase({ ...admin, database: 'master', poolMax: 1 });
  const result: InitDatabaseResult = {
    databaseCreated: false,
    loginCreated: false,
    userCreated: false,
  };
  try {
    const existing = await sql<{ id: number | null }>`SELECT DB_ID(${database}) AS id`
      .execute(master)
      .catch((error) => {
        throw connectionError(error, { ...admin, database: 'master' });
      });
    if (!existing.rows[0]?.id) {
      await sql.raw(`CREATE DATABASE [${database}]`).execute(master);
      result.databaseCreated = true;
    }
    // Lecturas sin bloquear a las escrituras: el bot y la pantalla de revisión trabajan a la vez.
    await sql
      .raw(`ALTER DATABASE [${database}] SET READ_COMMITTED_SNAPSHOT ON WITH ROLLBACK IMMEDIATE`)
      .execute(master);

    const login = await sql<{ id: number | null }>`SELECT SUSER_ID(${appLogin}) AS id`.execute(
      master,
    );
    if (!login.rows[0]?.id) {
      await sql
        .raw(
          `CREATE LOGIN [${appLogin}] WITH PASSWORD = ${quoteString(appPassword)}, CHECK_POLICY = ON, DEFAULT_DATABASE = [${database}]`,
        )
        .execute(master);
      result.loginCreated = true;
    }
  } finally {
    await master.destroy();
  }

  const target = createDatabase({ ...admin, database, poolMax: 1 });
  try {
    const user = await sql<{ id: number | null }>`SELECT USER_ID(${appLogin}) AS id`.execute(
      target,
    );
    if (!user.rows[0]?.id) {
      await sql.raw(`CREATE USER [${appLogin}] FOR LOGIN [${appLogin}]`).execute(target);
      result.userCreated = true;
    }
    await sql.raw(`ALTER ROLE db_owner ADD MEMBER [${appLogin}]`).execute(target);
  } finally {
    await target.destroy();
  }
  return result;
}

/**
 * Crea una base de datos vacía con la cuenta indicada, que queda como su
 * propietaria (la usan las pruebas, con un inicio de sesión dbcreator).
 */
export async function createEmptyDatabase(
  options: Omit<DbConnectionOptions, 'database'>,
  database: string,
  { replace = false } = {},
): Promise<void> {
  checkIdentifier(database, 'El nombre de la base de datos');
  const master = createDatabase({ ...options, database: 'master', poolMax: 1 });
  try {
    if (replace) await dropDatabaseWith(master, database);
    await sql.raw(`CREATE DATABASE [${database}]`).execute(master);
    await sql
      .raw(`ALTER DATABASE [${database}] SET READ_COMMITTED_SNAPSHOT ON WITH ROLLBACK IMMEDIATE`)
      .execute(master);
  } catch (error) {
    throw connectionError(error, { ...options, database: 'master' });
  } finally {
    await master.destroy();
  }
}

async function dropDatabaseWith(master: ReturnType<typeof createDatabase>, database: string) {
  await sql
    .raw(
      `IF DB_ID(N'${database}') IS NOT NULL BEGIN
         ALTER DATABASE [${database}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE;
         DROP DATABASE [${database}];
       END`,
    )
    .execute(master);
}

export async function dropDatabase(
  options: Omit<DbConnectionOptions, 'database'>,
  database: string,
): Promise<void> {
  checkIdentifier(database, 'El nombre de la base de datos');
  const master = createDatabase({ ...options, database: 'master', poolMax: 1 });
  try {
    await dropDatabaseWith(master, database);
  } finally {
    await master.destroy();
  }
}
