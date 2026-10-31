import fs from 'node:fs';
import path from 'node:path';
import Database from 'better-sqlite3';

/** Copias periódicas: invoicebot-AAAAMMDD-HHMMSS.db. Las de migración (invoicebot-vN-…) no se tocan. */
const BACKUP_PATTERN = /^invoicebot-(\d{8})-(\d{6})\.db$/;

function stamp(date) {
  const pad = (n) => String(n).padStart(2, '0');
  return (
    `${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}-` +
    `${pad(date.getHours())}${pad(date.getMinutes())}${pad(date.getSeconds())}`
  );
}

function parseStamp(name) {
  const m = BACKUP_PATTERN.exec(name);
  if (!m) return null;
  const [d, t] = [m[1], m[2]];
  return new Date(
    Number(d.slice(0, 4)),
    Number(d.slice(4, 6)) - 1,
    Number(d.slice(6, 8)),
    Number(t.slice(0, 2)),
    Number(t.slice(2, 4)),
    Number(t.slice(4, 6)),
  );
}

/** Copias periódicas existentes, de la más reciente a la más antigua. */
export function listBackups(dir) {
  if (!fs.existsSync(dir)) return [];
  return fs
    .readdirSync(dir)
    .map((name) => ({ name, path: path.join(dir, name), createdAt: parseStamp(name) }))
    .filter((b) => b.createdAt)
    .sort((a, b) => b.createdAt - a.createdAt);
}

export function isBackupDue(dir, intervalHours, now = new Date()) {
  const [latest] = listBackups(dir);
  return !latest || now - latest.createdAt >= intervalHours * 3600 * 1000;
}

/**
 * Copia consistente de la base de datos (VACUUM INTO funciona con la base
 * abierta y en uso), verificada con integrity_check. Después conserva solo
 * las `retention` copias periódicas más recientes.
 *
 * @returns {{ path: string, size: number, removed: string[] }}
 */
export function createBackup(db, dir, { retention, now = new Date() }) {
  fs.mkdirSync(dir, { recursive: true });
  let target = path.join(dir, `invoicebot-${stamp(now)}.db`);
  for (let i = 1; fs.existsSync(target); i++) {
    // Dos copias en el mismo segundo: no sobrescribir la anterior.
    target = path.join(dir, `invoicebot-${stamp(new Date(now.getTime() + i * 1000))}.db`);
  }
  db.prepare('VACUUM INTO ?').run(target);

  const copy = new Database(target, { readonly: true });
  try {
    const result = copy.pragma('integrity_check', { simple: true });
    if (result !== 'ok')
      throw new Error(`La copia no superó la verificación de integridad: ${result}`);
  } finally {
    copy.close();
  }

  const removed = [];
  for (const old of listBackups(dir).slice(retention)) {
    fs.rmSync(old.path, { force: true });
    removed.push(old.name);
  }
  return { path: target, size: fs.statSync(target).size, removed };
}
