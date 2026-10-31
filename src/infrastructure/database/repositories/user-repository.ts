import crypto from 'node:crypto';
import { sql } from 'kysely';
import type { Kysely } from 'kysely';
import type { Database, UserRow } from '../schema.ts';

const utcNow = sql<string>`SYSUTCDATETIME()`;

export type PublicUser = Omit<UserRow, 'password_hash'>;

export interface ReviewSession {
  userId: number;
  username: string;
  displayName: string;
  csrf: string;
}

const PUBLIC_COLUMNS = [
  'id',
  'username',
  'display_name',
  'active',
  'created_at',
  'updated_at',
  'last_login_at',
] as const;

const hashToken = (token: string) => crypto.createHash('sha256').update(token).digest();

/** Usuarios y sesiones de la pantalla de revisión en red. */
export class UserRepository {
  readonly db: Kysely<Database>;

  constructor(db: Kysely<Database>) {
    this.db = db;
  }

  /** Incluye password_hash: solo para autenticar. */
  async findByUsername(username: string): Promise<UserRow | null> {
    return (
      (await this.db
        .selectFrom('users')
        .selectAll()
        .where('username', '=', username)
        .executeTakeFirst()) ?? null
    );
  }

  async getById(id: number): Promise<PublicUser | null> {
    return (
      (await this.db
        .selectFrom('users')
        .select(PUBLIC_COLUMNS)
        .where('id', '=', id)
        .executeTakeFirst()) ?? null
    );
  }

  async list(): Promise<PublicUser[]> {
    return this.db.selectFrom('users').select(PUBLIC_COLUMNS).orderBy('username').execute();
  }

  async countActive(): Promise<number> {
    const row = await this.db
      .selectFrom('users')
      .select((eb) => eb.fn.countAll<number>().as('n'))
      .where('active', '=', true)
      .executeTakeFirstOrThrow();
    return Number(row.n);
  }

  async insert({
    username,
    displayName,
    passwordHash,
  }: {
    username: string;
    displayName: string;
    passwordHash: string;
  }): Promise<number> {
    const row = await this.db
      .insertInto('users')
      .values({ username, display_name: displayName, password_hash: passwordHash })
      .output('inserted.id')
      .executeTakeFirstOrThrow();
    return row.id;
  }

  /** Cambiar la contraseña o el estado actualiza updated_at, lo que invalida las sesiones abiertas. */
  async setPassword(id: number, passwordHash: string): Promise<void> {
    await this.db
      .updateTable('users')
      .set({ password_hash: passwordHash, updated_at: utcNow })
      .where('id', '=', id)
      .execute();
    await this.db.deleteFrom('review_sessions').where('user_id', '=', id).execute();
  }

  async setActive(id: number, active: boolean): Promise<void> {
    await this.db
      .updateTable('users')
      .set({ active, updated_at: utcNow })
      .where('id', '=', id)
      .execute();
    if (!active) await this.db.deleteFrom('review_sessions').where('user_id', '=', id).execute();
  }

  async touchLogin(id: number): Promise<void> {
    await this.db
      .updateTable('users')
      .set({ last_login_at: utcNow })
      .where('id', '=', id)
      .execute();
  }

  // --- Sesiones ------------------------------------------------------------

  /** Crea una sesión y devuelve su identificador (solo se guarda su hash). */
  async createSession(
    user: PublicUser,
    { hours, ip }: { hours: number; ip: string | null },
  ): Promise<string> {
    const token = crypto.randomBytes(32).toString('base64url');
    await this.db.deleteFrom('review_sessions').where('expires_at', '<', utcNow).execute();
    await this.db
      .insertInto('review_sessions')
      .values({
        token_hash: hashToken(token),
        user_id: user.id,
        user_version: user.updated_at,
        csrf_token: crypto.randomBytes(24).toString('hex'),
        ip,
        expires_at: sql<string>`DATEADD(MINUTE, ${Math.round(hours * 60)}, SYSUTCDATETIME())`,
      })
      .execute();
    return token;
  }

  /**
   * Sesión vigente: no caducada, de un usuario activo y sin cambios desde
   * que entró (contraseña cambiada o cuenta desactivada la invalidan).
   */
  async getSession(token: string | undefined): Promise<ReviewSession | null> {
    if (!token || token.length > 100) return null;
    const row = await this.db
      .selectFrom('review_sessions as s')
      .innerJoin('users as u', 'u.id', 's.user_id')
      .select(['s.user_id', 's.csrf_token', 'u.username', 'u.display_name'])
      .where('s.token_hash', '=', hashToken(token))
      .where('s.expires_at', '>', utcNow)
      .where('u.active', '=', true)
      .whereRef('u.updated_at', '=', 's.user_version')
      .executeTakeFirst();
    if (!row) return null;
    return {
      userId: row.user_id,
      username: row.username,
      displayName: row.display_name,
      csrf: row.csrf_token,
    };
  }

  async deleteSession(token: string | undefined): Promise<void> {
    if (!token) return;
    await this.db
      .deleteFrom('review_sessions')
      .where('token_hash', '=', hashToken(token))
      .execute();
  }
}
