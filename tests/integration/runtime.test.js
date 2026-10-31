import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import Database from 'better-sqlite3';
import { afterEach, describe, expect, it } from 'vitest';
import { openDatabase } from '../../src/infrastructure/database/connection.js';
import {
  createBackup,
  isBackupDue,
  listBackups,
} from '../../src/infrastructure/database/backup.js';
import {
  createDailyFileStream,
  createLogger,
  pruneLogFiles,
} from '../../src/infrastructure/logging/logger.js';
import { LockedError, acquireLock } from '../../src/infrastructure/runtime/lock.js';
import { makeTempDir } from '../helpers/context.js';

const dirs = [];
const tempDir = () => {
  const dir = makeTempDir();
  dirs.push(dir);
  return dir;
};
afterEach(() => {
  for (const dir of dirs.splice(0)) fs.rmSync(dir, { recursive: true, force: true });
});

describe('copias de seguridad', () => {
  it('crea copias verificadas y conserva solo las más recientes', () => {
    const dir = tempDir();
    const db = openDatabase(path.join(dir, 'invoicebot.db'));
    db.prepare(
      "INSERT INTO bot_runs (trigger, status, started_at) VALUES ('manual', 'success', '2026-10-30')",
    ).run();
    const backups = path.join(dir, 'backups');
    // Una copia de migración que nunca debe borrarse.
    fs.mkdirSync(backups);
    fs.writeFileSync(path.join(backups, 'invoicebot-v1-2026-10-01.db'), '');

    const days = [1, 2, 3, 4].map((d) => new Date(2026, 9, d, 8, 0, 0));
    const results = days.map((now) => createBackup(db, backups, { retention: 2, now }));
    db.close();

    expect(listBackups(backups).map((b) => b.name)).toEqual([
      'invoicebot-20261004-080000.db',
      'invoicebot-20261003-080000.db',
    ]);
    expect(results[3].removed).toEqual(['invoicebot-20261002-080000.db']);
    expect(fs.existsSync(path.join(backups, 'invoicebot-v1-2026-10-01.db'))).toBe(true);

    const copy = new Database(results[3].path, { readonly: true });
    expect(copy.prepare('SELECT COUNT(*) FROM bot_runs').pluck().get()).toBe(1);
    copy.close();
  });

  it('no sobrescribe dos copias hechas en el mismo segundo', () => {
    const dir = tempDir();
    const db = openDatabase(path.join(dir, 'invoicebot.db'));
    const now = new Date(2026, 9, 30, 10, 0, 0);
    const a = createBackup(db, dir, { retention: 5, now });
    const b = createBackup(db, dir, { retention: 5, now });
    db.close();
    expect(a.path).not.toBe(b.path);
  });

  it('decide si toca una copia según el intervalo', () => {
    const dir = tempDir();
    expect(isBackupDue(dir, 24)).toBe(true);
    fs.writeFileSync(path.join(dir, 'invoicebot-20261030-080000.db'), '');
    expect(isBackupDue(dir, 24, new Date(2026, 9, 30, 20, 0, 0))).toBe(false);
    expect(isBackupDue(dir, 24, new Date(2026, 9, 31, 8, 0, 1))).toBe(true);
  });
});

describe('logs en archivo', () => {
  it('escribe un archivo por día y cambia de archivo al cambiar la fecha', () => {
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

  it('borra los archivos más antiguos que la retención y no toca otros archivos', () => {
    const dir = tempDir();
    for (const name of ['invoicebot-2026-09-01.log', 'invoicebot-2026-10-25.log', 'notas.txt']) {
      fs.writeFileSync(path.join(dir, name), '');
    }
    const removed = pruneLogFiles(dir, 30, new Date(2026, 9, 30));
    expect(removed).toEqual(['invoicebot-2026-09-01.log']);
    expect(fs.readdirSync(dir).sort()).toEqual(['invoicebot-2026-10-25.log', 'notas.txt']);
  });

  it('registra en archivo al menos el nivel info y oculta datos sensibles', () => {
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
  it('impide una segunda ejecución mientras la primera sigue viva', () => {
    const lockPath = path.join(tempDir(), 'invoicebot.lock');
    const release = acquireLock(lockPath);
    expect(() => acquireLock(lockPath)).toThrow(LockedError);
    release();
    release(); // liberar dos veces no falla
    acquireLock(lockPath)();
  });

  it('recupera un candado abandonado por un proceso que ya no existe', () => {
    const lockPath = path.join(tempDir(), 'invoicebot.lock');
    fs.writeFileSync(
      lockPath,
      JSON.stringify({ pid: 2147483646, host: os.hostname(), startedAt: new Date().toISOString() }),
    );
    expect(() => acquireLock(lockPath)()).not.toThrow();
  });

  it('recupera un candado demasiado antiguo aunque sea de otro equipo', () => {
    const lockPath = path.join(tempDir(), 'invoicebot.lock');
    fs.writeFileSync(
      lockPath,
      JSON.stringify({ pid: 1, host: 'otro-equipo', startedAt: '2026-10-29T00:00:00.000Z' }),
    );
    const now = () => Date.parse('2026-10-30T12:00:00.000Z');
    expect(() => acquireLock(lockPath, { now })()).not.toThrow();
    fs.writeFileSync(
      lockPath,
      JSON.stringify({ pid: 1, host: 'otro-equipo', startedAt: '2026-10-30T11:00:00.000Z' }),
    );
    expect(() => acquireLock(lockPath, { now })).toThrow(LockedError);
  });
});
