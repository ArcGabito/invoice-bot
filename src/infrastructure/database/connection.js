import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import Database from 'better-sqlite3';

const MIGRATIONS_DIR = path.join(path.dirname(fileURLToPath(import.meta.url)), 'migrations');

/**
 * Abre (o crea) la base de datos y aplica las migraciones pendientes.
 * Usa ':memory:' en pruebas.
 */
export function openDatabase(dbPath) {
  if (dbPath !== ':memory:') fs.mkdirSync(path.dirname(dbPath), { recursive: true });
  const db = new Database(dbPath);
  db.pragma('foreign_keys = ON');
  db.pragma('busy_timeout = 5000');
  if (dbPath !== ':memory:') db.pragma('journal_mode = WAL');
  runMigrations(db);
  return db;
}

export function listMigrations() {
  return fs
    .readdirSync(MIGRATIONS_DIR)
    .filter((f) => /^\d+_.+\.sql$/.test(f))
    .sort()
    .map((file) => ({
      version: Number(file.split('_')[0]),
      name: file,
      file: path.join(MIGRATIONS_DIR, file),
    }));
}

export function runMigrations(db) {
  db.exec(`CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    applied_at TEXT NOT NULL
  )`);
  const applied = new Set(db.prepare('SELECT version FROM schema_migrations').pluck().all());
  const record = db.prepare(
    'INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)',
  );
  const pending = listMigrations().filter((m) => !applied.has(m.version));
  for (const migration of pending) {
    const sql = fs.readFileSync(migration.file, 'utf8');
    db.transaction(() => {
      db.exec(sql);
      record.run(migration.version, migration.name, new Date().toISOString());
    })();
  }
  return pending.map((m) => m.name);
}
