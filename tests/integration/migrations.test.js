import fs from 'node:fs';
import path from 'node:path';
import Database from 'better-sqlite3';
import { afterEach, describe, expect, it } from 'vitest';
import {
  listMigrations,
  openDatabase,
  runMigrations,
} from '../../src/infrastructure/database/connection.js';
import { makeTempDir } from '../helpers/context.js';

const dirs = [];
afterEach(() => {
  for (const dir of dirs.splice(0)) fs.rmSync(dir, { recursive: true, force: true });
});

/** Crea una base de datos en el esquema de v0.1.0 (solo la migración 001) con datos. */
function createV1Database(dbPath) {
  const db = new Database(dbPath);
  db.pragma('foreign_keys = ON');
  runMigrations(db, { upTo: 1 });
  const ts = '2026-10-01T00:00:00.000Z';
  db.prepare(
    `INSERT INTO documents (id, sha256, file_path, file_name, mime_type, file_size, status, text_method, created_at, updated_at)
     VALUES (1, 'abc', 'C:/f/a.pdf', 'a.pdf', 'application/pdf', 10, 'extracted', 'pdf_text', ?, ?)`,
  ).run(ts, ts);
  db.prepare(
    "INSERT INTO file_occurrences (document_id, file_path, first_seen_at, last_seen_at) VALUES (1, 'C:/f/a.pdf', ?, ?)",
  ).run(ts, ts);
  db.prepare(
    `INSERT INTO extraction_runs (id, document_id, attempt, provider, prompt_version, status, created_at)
     VALUES (1, 1, 1, 'mock', 'v1', 'success', ?)`,
  ).run(ts);
  db.prepare(
    `INSERT INTO invoices (id, document_id, extraction_run_id, total_cents, validation_status, rules_version, created_at, updated_at)
     VALUES (1, 1, 1, 11800, 'valid', 'r1', ?, ?)`,
  ).run(ts, ts);
  db.prepare(
    "INSERT INTO validation_issues (invoice_id, rule_code, severity, message) VALUES (1, 'X', 'info', 'nota')",
  ).run();
  db.close();
}

describe('migraciones', () => {
  it('actualiza una base de datos de v0.1.0 sin perder datos y deja una copia de seguridad', () => {
    const dir = makeTempDir();
    dirs.push(dir);
    const dbPath = path.join(dir, 'invoicebot.db');
    createV1Database(dbPath);

    const db = openDatabase(dbPath);
    const versions = db
      .prepare('SELECT version FROM schema_migrations ORDER BY version')
      .pluck()
      .all();
    expect(versions).toEqual(listMigrations().map((m) => m.version));

    // Los datos y sus relaciones sobreviven a la reconstrucción de documents.
    expect(db.prepare('SELECT COUNT(*) FROM file_occurrences').pluck().get()).toBe(1);
    expect(db.prepare('SELECT COUNT(*) FROM validation_issues').pluck().get()).toBe(1);
    expect(db.prepare('SELECT data_source, total_cents FROM invoices').get()).toEqual({
      data_source: 'ai',
      total_cents: 11800,
    });
    expect(db.pragma('foreign_keys', { simple: true })).toBe(1);
    expect(db.pragma('foreign_key_check')).toEqual([]);

    // El nuevo esquema acepta los estados y métodos añadidos.
    db.prepare(
      "UPDATE documents SET status = 'skipped', text_method = 'xml', skip_reason = 'cdr' WHERE id = 1",
    ).run();
    expect(() =>
      db.prepare("UPDATE documents SET status = 'inventado' WHERE id = 1").run(),
    ).toThrow(/CHECK/);
    // Las claves foráneas siguen activas y en cascada.
    db.prepare('DELETE FROM documents WHERE id = 1').run();
    expect(db.prepare('SELECT COUNT(*) FROM invoices').pluck().get()).toBe(0);
    db.close();

    const backups = fs.readdirSync(path.join(dir, 'backups'));
    expect(backups).toHaveLength(1);
    expect(backups[0]).toMatch(/^invoicebot-v1-.*\.db$/);
    const backup = new Database(path.join(dir, 'backups', backups[0]), { readonly: true });
    expect(backup.prepare('SELECT COUNT(*) FROM invoices').pluck().get()).toBe(1);
    backup.close();
  });

  it('no crea copias de seguridad para una base de datos nueva ni si no hay migraciones pendientes', () => {
    const dir = makeTempDir();
    dirs.push(dir);
    const dbPath = path.join(dir, 'invoicebot.db');
    openDatabase(dbPath).close();
    openDatabase(dbPath).close();
    expect(fs.existsSync(path.join(dir, 'backups'))).toBe(false);
  });
});
