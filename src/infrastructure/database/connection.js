import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import Database from 'better-sqlite3';

const MIGRATIONS_DIR = path.join(path.dirname(fileURLToPath(import.meta.url)), 'migrations');

/**
 * Abre (o crea) la base de datos y aplica las migraciones pendientes.
 * Usa ':memory:' en pruebas.
 */
export function openDatabase(dbPath, { backupDir = null } = {}) {
  const inMemory = dbPath === ':memory:';
  if (!inMemory) fs.mkdirSync(path.dirname(dbPath), { recursive: true });
  const db = new Database(dbPath);
  db.pragma('foreign_keys = ON');
  db.pragma('busy_timeout = 5000');
  if (!inMemory) db.pragma('journal_mode = WAL');
  runMigrations(db, {
    backupDir: inMemory ? null : (backupDir ?? path.join(path.dirname(dbPath), 'backups')),
  });
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

/**
 * Aplica las migraciones pendientes, cada una en su transacción.
 *
 * Si la base de datos ya tenía datos, antes guarda una copia completa en
 * `backupDir`: una actualización nunca debe poder perder información.
 * Las claves foráneas se desactivan durante la migración porque reconstruir
 * una tabla (la forma de cambiar un CHECK en SQLite) borraría en cascada sus
 * filas dependientes; al terminar se verifica la integridad referencial.
 */
export function runMigrations(db, { backupDir = null, upTo = Infinity } = {}) {
  db.exec(`CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    applied_at TEXT NOT NULL
  )`);
  const applied = new Set(db.prepare('SELECT version FROM schema_migrations').pluck().all());
  const pending = listMigrations().filter((m) => !applied.has(m.version) && m.version <= upTo);
  if (pending.length === 0) return { applied: [], backup: null };

  let backup = null;
  if (backupDir && applied.size > 0) {
    fs.mkdirSync(backupDir, { recursive: true });
    const current = Math.max(...applied);
    const stamp = new Date().toISOString().replace(/[:.]/g, '-');
    backup = path.join(backupDir, `invoicebot-v${current}-${stamp}.db`);
    db.prepare('VACUUM INTO ?').run(backup);
  }

  const record = db.prepare(
    'INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)',
  );
  db.pragma('foreign_keys = OFF');
  try {
    for (const migration of pending) {
      const sql = fs.readFileSync(migration.file, 'utf8');
      db.transaction(() => {
        db.exec(sql);
        const violations = db.pragma('foreign_key_check');
        if (violations.length) {
          throw new Error(`La migración ${migration.name} rompe ${violations.length} referencias`);
        }
        record.run(migration.version, migration.name, new Date().toISOString());
      })();
    }
  } finally {
    db.pragma('foreign_keys = ON');
  }
  return { applied: pending.map((m) => m.name), backup };
}
