import fs from 'node:fs';
import path from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import {
  createBackup,
  isBackupDue,
  listBackups,
} from '../../src/infrastructure/database/backup.ts';
import {
  createDailyFileStream,
  createLogger,
  pruneLogFiles,
} from '../../src/infrastructure/logging/logger.ts';
import { LockedError, acquireLock } from '../../src/infrastructure/runtime/lock.ts';
import { makeTempDir } from '../helpers/context.ts';
import { resetDatabase, sqlRun, workerDatabase, workerDatabaseName } from '../helpers/db.ts';

const dirs: string[] = [];
const tempDir = () => {
  const dir = makeTempDir();
  dirs.push(dir);
  return dir;
};
afterEach(async () => {
  for (const dir of dirs.splice(0)) fs.rmSync(dir, { recursive: true, force: true });
});

/** Base del proceso de pruebas, vacía. */
async function emptyDatabase() {
  const db = await workerDatabase();
  await resetDatabase(db);
  return db;
}

describe('copias de seguridad', () => {
  it('crea copias verificadas en el servidor y las registra en su historial', async () => {
    const db = await emptyDatabase();
    const periodic = await createBackup(db, null, { now: new Date(Date.UTC(2026, 9, 1, 8, 0, 0)) });
    // La ruta es del servidor (puede ser Windows o Linux); el archivo lleva el nombre de la base.
    expect(periodic.path.split(/[\\/]/).pop()).toBe(`${workerDatabaseName()}-20261001-080000.bak`);
    // Las copias previas a una migración no son periódicas: no cuentan para el intervalo.
    const premigration = await createBackup(db, null, { label: 'v1' });
    expect(premigration.path).toMatch(/invoicebot_test_\w+-v1-\d{8}-\d{6}\.bak$/);

    const listed = (await listBackups(db)).map((b) => b.path);
    expect(listed).toContain(periodic.path);
    expect(listed).not.toContain(premigration.path);
  });

  it('decide si toca una copia según la última del historial', async () => {
    const db = await emptyDatabase();
    await createBackup(db, null);
    const [latest] = await listBackups(db, 1);
    // La hora del historial está en UTC (msdb guarda la hora local del servidor).
    expect(Math.abs(latest!.createdAt.getTime() - Date.now())).toBeLessThan(5 * 60_000);
    expect(await isBackupDue(db, 24)).toBe(false);
    expect(await isBackupDue(db, 24, new Date(Date.now() + 25 * 3600_000))).toBe(true);
  });
});

describe('logs en archivo', () => {
  it('escribe un archivo por día y cambia de archivo al cambiar la fecha', async () => {
    const dir = tempDir();
    let now = new Date(2026, 9, 30, 23, 59);
    const stream = createDailyFileStream(dir, { now: () => now });
    stream.write('a\n');
    now = new Date(2026, 9, 31, 0, 1);
    stream.write('b\n');
    expect(fs.readdirSync(dir).sort()).toEqual([
      'invoicebot-2026-10-30.log',
      'invoicebot-2026-10-31.log',
    ]);
  });

  it('borra los archivos más antiguos que la retención y no toca otros archivos', async () => {
    const dir = tempDir();
    for (const name of ['invoicebot-2026-09-01.log', 'invoicebot-2026-10-25.log', 'notas.txt']) {
      fs.writeFileSync(path.join(dir, name), '');
    }
    const removed = pruneLogFiles(dir, 30, new Date(2026, 9, 30));
    expect(removed).toEqual(['invoicebot-2026-09-01.log']);
    expect(fs.readdirSync(dir).sort()).toEqual(['invoicebot-2026-10-25.log', 'notas.txt']);
  });

  it('registra en archivo al menos el nivel info y oculta datos sensibles', async () => {
    const dir = tempDir();
    const logger = createLogger({ level: 'silent', fileDir: dir });
    logger.info({ doc: { text: 'contenido de la factura', id: 7 } }, 'documento procesado');
    logger.debug('esto no se guarda');
    const [file] = fs.readdirSync(dir);
    const lines = fs
      .readFileSync(path.join(dir, file), 'utf8')
      .trim()
      .split('\n')
      .map((l) => JSON.parse(l));
    expect(lines).toHaveLength(1);
    expect(lines[0]).toMatchObject({
      msg: 'documento procesado',
      doc: { text: '[redactado]', id: 7 },
    });
    expect(lines[0].time).toMatch(/^\d{4}-\d{2}-\d{2}T/);
  });
});

describe('candado de ejecución', () => {
  it('impide una segunda ejecución mientras la primera sigue viva', async () => {
    const db = await emptyDatabase();
    const release = await acquireLock(db);
    await expect(acquireLock(db)).rejects.toThrow(LockedError);
    await release();
    const again = await acquireLock(db);
    await again();
  });

  it('dice quién tiene el candado según la ejecución en curso', async () => {
    const db = await emptyDatabase();
    await sqlRun(
      db,
      "INSERT INTO bot_runs ([trigger], status, host, pid) VALUES (N'watch', N'running', N'vm-bot', 4321)",
    );
    const release = await acquireLock(db);
    try {
      const error = await acquireLock(db).catch((e: unknown) => e);
      expect(error).toBeInstanceOf(LockedError);
      expect((error as LockedError).message).toMatch(/PID 4321 en vm-bot/);
    } finally {
      await release();
    }
  });
});
