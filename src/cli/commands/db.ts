import type { Command } from 'commander';
import { initDatabase } from '../../infrastructure/database/admin.ts';
import {
  databaseOptions,
  describeDatabase,
  openDatabase,
  schemaVersion,
} from '../../infrastructure/database/connection.ts';
import { MIGRATIONS } from '../../infrastructure/database/migrations.ts';
import { ConfigError, EXIT_CODES } from '../../shared/errors.ts';
import type { Runner } from '../index.ts';

const LATEST_VERSION = Math.max(...MIGRATIONS.map((m) => m.version));

export function registerDb(program: Command, run: Runner): void {
  const db = program
    .command('db')
    .description('Base de datos SQL Server: creación, migraciones y estado');

  db.command('init')
    .description(
      'Crea la base de datos y el usuario de InvoiceBot con una cuenta de administrador (INVOICEBOT_DB_ADMIN_USER / _PASSWORD)',
    )
    .action(() =>
      run(
        async (ctx, out) => {
          const app = databaseOptions(ctx.config);
          const adminUser = process.env.INVOICEBOT_DB_ADMIN_USER;
          const adminPassword = process.env.INVOICEBOT_DB_ADMIN_PASSWORD;
          if (!adminUser || !adminPassword) {
            throw new ConfigError(
              'Define INVOICEBOT_DB_ADMIN_USER e INVOICEBOT_DB_ADMIN_PASSWORD (una cuenta con permiso para crear bases de datos e inicios de sesión, p. ej. sa). Solo se usan en este comando: quítalas del .env al terminar.',
            );
          }
          const result = await initDatabase({
            admin: { ...app, user: adminUser, password: adminPassword },
            database: app.database,
            appLogin: app.user,
            appPassword: app.password,
          });
          const migration = await ctx.connect();
          out.data({ database: describeDatabase(ctx.config), ...result, migration });
          out.line(`Base de datos ${describeDatabase(ctx.config)}`);
          out.line(`  Base de datos: ${result.databaseCreated ? 'creada' : 'ya existía'}`);
          out.line(
            `  Inicio de sesión ${app.user}: ${result.loginCreated ? 'creado' : 'ya existía (no se cambió su contraseña)'}`,
          );
          out.line(
            `  Usuario en la base de datos: ${result.userCreated ? 'creado' : 'ya existía'} (db_owner)`,
          );
          out.line(
            `  Migraciones aplicadas: ${migration?.applied.join(', ') || 'ninguna pendiente'}`,
          );
          out.line();
          out.line(
            'Ya puedes quitar INVOICEBOT_DB_ADMIN_USER e INVOICEBOT_DB_ADMIN_PASSWORD del .env.',
          );
          return EXIT_CODES.OK;
        },
        { database: false },
      ),
    );

  db.command('migrate')
    .description('Aplica las migraciones pendientes (haciendo antes una copia de seguridad)')
    .action(() =>
      run(
        async (ctx, out) => {
          const migration = await ctx.connect();
          out.data({ database: describeDatabase(ctx.config), migration, version: LATEST_VERSION });
          if (migration?.applied.length) {
            out.line(`Migraciones aplicadas: ${migration.applied.join(', ')}`);
            if (migration.backup) out.line(`Copia previa: ${migration.backup}`);
          } else {
            out.line(`El esquema ya está al día (versión ${LATEST_VERSION})`);
          }
          return EXIT_CODES.OK;
        },
        { database: false },
      ),
    );

  db.command('status')
    .description('Muestra la conexión y la versión del esquema, sin cambiar nada')
    .action(() =>
      run(
        async (ctx, out) => {
          const { db: conn } = await openDatabase(databaseOptions(ctx.config), { migrate: false });
          try {
            const version = await schemaVersion(conn);
            const pending = MIGRATIONS.filter((m) => m.version > version).map((m) => m.name);
            out.data({
              database: describeDatabase(ctx.config),
              version,
              latest: LATEST_VERSION,
              pending,
            });
            out.line(`Base de datos: ${describeDatabase(ctx.config)}`);
            out.line(`Esquema: versión ${version} de ${LATEST_VERSION}`);
            if (pending.length)
              out.line(`Pendientes: ${pending.join(', ')} (se aplican con: invoicebot db migrate)`);
            return EXIT_CODES.OK;
          } finally {
            await conn.destroy();
          }
        },
        { database: false },
      ),
    );
}
