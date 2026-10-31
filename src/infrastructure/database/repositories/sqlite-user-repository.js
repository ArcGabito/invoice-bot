const now = () => new Date().toISOString();

const PUBLIC_COLUMNS = 'id, username, display_name, active, created_at, updated_at, last_login_at';

/** Usuarios de la pantalla de revisión en red. */
export class SqliteUserRepository {
  /** @param {import('better-sqlite3').Database} db */
  constructor(db) {
    this.db = db;
  }

  /** Incluye password_hash: solo para autenticar. */
  findByUsername(username) {
    return this.db.prepare('SELECT * FROM users WHERE username = ?').get(username) ?? null;
  }

  getById(id) {
    return this.db.prepare(`SELECT ${PUBLIC_COLUMNS} FROM users WHERE id = ?`).get(id) ?? null;
  }

  list() {
    return this.db.prepare(`SELECT ${PUBLIC_COLUMNS} FROM users ORDER BY username`).all();
  }

  countActive() {
    return this.db.prepare('SELECT COUNT(*) FROM users WHERE active = 1').pluck().get();
  }

  insert({ username, displayName, passwordHash }) {
    const ts = now();
    return Number(
      this.db
        .prepare(
          `INSERT INTO users (username, display_name, password_hash, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?)`,
        )
        .run(username, displayName, passwordHash, ts, ts).lastInsertRowid,
    );
  }

  setPassword(id, passwordHash) {
    this.db
      .prepare('UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?')
      .run(passwordHash, now(), id);
  }

  setActive(id, active) {
    this.db
      .prepare('UPDATE users SET active = ?, updated_at = ? WHERE id = ?')
      .run(active ? 1 : 0, now(), id);
  }

  touchLogin(id) {
    this.db.prepare('UPDATE users SET last_login_at = ? WHERE id = ?').run(now(), id);
  }
}
