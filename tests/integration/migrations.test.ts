import fs from 'node:fs';
import path from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { scanInvoices } from '../../src/application/use-cases/scan-invoices.ts';
import { createEmptyDatabase, dropDatabase } from '../../src/infrastructure/database/admin.ts';
import {
  createDatabase,
  runMigrations,
  schemaVersion,
} from '../../src/infrastructure/database/connection.ts';
import type { Db } from '../../src/infrastructure/database/connection.ts';
import { MIGRATIONS } from '../../src/infrastructure/database/migrations.ts';
import { buildUblInvoice, writeUbl } from '../helpers/ubl-builder.ts';
import { makeTestDeps } from '../helpers/context.ts';
import { countRows, requireServer, sqlColumn, sqlRun, workerDatabaseName } from '../helpers/db.ts';

const LATEST = Math.max(...MIGRATIONS.map((m) => m.version));

describe('migraciones', () => {
  // Base propia, vacía en cada prueba (la del proceso ya está migrada).
  const name = `${workerDatabaseName()}_mig`;
  let db: Db;

  beforeEach(async () => {
    const server = requireServer();
    await createEmptyDatabase(server, name, { replace: true });
    db = createDatabase({ ...server, database: name, poolMax: 2 });
  });
  afterEach(async () => {
    await db.destroy();
    await dropDatabase(requireServer(), name);
  });

  it('crea el esquema completo en una base vacía, sin copia de seguridad', async () => {
    const backup = vi.fn(async (label: string) => `copia-${label}`);
    expect(await schemaVersion(db)).toBe(0);

    const result = await runMigrations(db, { backup });
    expect(result).toEqual({ applied: MIGRATIONS.map((m) => m.name), backup: null });
    expect(await schemaVersion(db)).toBe(LATEST);
    expect(await sqlColumn(db, 'SELECT version FROM schema_migrations ORDER BY version')).toEqual(
      MIGRATIONS.map((m) => m.version),
    );

    // Sin migraciones pendientes no hace nada.
    expect(await runMigrations(db, { backup })).toEqual({ applied: [], backup: null });
    expect(backup).not.toHaveBeenCalled();
  });

  it('hace una copia de seguridad antes de actualizar una base que ya tenía esquema', async () => {
    // Una base en una versión anterior: tiene registrada una migración previa.
    await runMigrations(db, { upTo: 0 });
    await sqlRun(db, "INSERT INTO schema_migrations (version, name) VALUES (0, N'000_previa')");
    const backup = vi.fn(async (label: string) => `copia-${label}`);

    const result = await runMigrations(db, { backup });
    expect(backup).toHaveBeenCalledExactlyOnceWith('v0');
    expect(result.backup).toBe('copia-v0');
    expect(await schemaVersion(db)).toBe(LATEST);
  });

  it('si la copia previa falla no migra', async () => {
    await runMigrations(db, { upTo: 0 });
    await sqlRun(db, "INSERT INTO schema_migrations (version, name) VALUES (0, N'000_previa')");
    await expect(
      runMigrations(db, { backup: () => Promise.reject(new Error('disco lleno')) }),
    ).rejects.toThrow('disco lleno');
    expect(await schemaVersion(db)).toBe(0);
  });
});

describe('esquema', () => {
  it('rechaza estados desconocidos y borra en cascada lo que depende de un documento', async () => {
    const env = await makeTestDeps();
    try {
      writeUbl(path.join(env.dir, 'f.xml'), buildUblInvoice());
      await scanInvoices({ root: env.dir, deps: env.deps });
      expect(await countRows(env.db, 'invoices')).toBe(1);

      await expect(sqlRun(env.db, "UPDATE documents SET status = N'inventado'")).rejects.toThrow(
        /CHECK/,
      );

      await sqlRun(env.db, 'DELETE FROM documents');
      for (const table of [
        'file_occurrences',
        'extraction_runs',
        'invoices',
        'validation_issues',
      ]) {
        expect(await countRows(env.db, table)).toBe(0);
      }
    } finally {
      fs.rmSync(env.dir, { recursive: true, force: true });
    }
  });
});
