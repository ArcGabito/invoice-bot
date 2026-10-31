import { sql } from 'kysely';
import type { Db } from '../database/connection.ts';
import { InvoiceBotError } from '../../shared/errors.ts';

export interface LockHolder {
  host: string | null;
  pid: number | null;
  startedAt: string | null;
}

export class LockedError extends InvoiceBotError {
  holder: LockHolder;

  constructor(holder: LockHolder) {
    const who = holder.host
      ? ` (PID ${holder.pid} en ${holder.host}, desde ${holder.startedAt})`
      : '';
    super(`Ya hay una ejecución en curso${who}`, { code: 'LOCKED' });
    this.holder = holder;
  }
}

const LOCK_NAME = 'invoicebot:run';

/**
 * Candado de una sola ejecución con sp_getapplock, de sesión: lo garantiza
 * SQL Server aunque el bot corra en varias máquinas o contenedores, y se
 * libera solo si el proceso muere (se cierra su conexión). No hay candados
 * abandonados que recuperar.
 *
 * @returns función que libera el candado y devuelve la conexión
 */
export async function acquireLock(db: Db): Promise<() => Promise<void>> {
  let release!: () => void;
  const released = new Promise<void>((resolve) => (release = resolve));
  let acquired!: (ok: boolean) => void;
  let failed!: (error: unknown) => void;
  const outcome = new Promise<boolean>((resolve, reject) => {
    acquired = resolve;
    failed = reject;
  });

  // La conexión queda reservada mientras dure la ejecución: el candado vive con ella.
  const holding = db
    .connection()
    .execute(async (conn) => {
      const result = await sql<{ result: number }>`
        DECLARE @result INT;
        EXEC @result = sp_getapplock @Resource = ${LOCK_NAME}, @LockMode = 'Exclusive',
          @LockOwner = 'Session', @LockTimeout = 0;
        SELECT @result AS result`.execute(conn);
      const ok = (result.rows[0]?.result ?? -1) >= 0;
      acquired(ok);
      if (!ok) return;
      await released;
      await sql`EXEC sp_releaseapplock @Resource = ${LOCK_NAME}, @LockOwner = 'Session'`.execute(
        conn,
      );
    })
    .catch((error: unknown) => failed(error));

  if (!(await outcome)) {
    await holding;
    throw new LockedError(await currentHolder(db));
  }
  return async () => {
    release();
    await holding;
  };
}

/** Quién tiene el candado, según la ejecución "running" más reciente de bot_runs. */
async function currentHolder(db: Db): Promise<LockHolder> {
  const run = await db
    .selectFrom('bot_runs')
    .select(['host', 'pid', 'started_at'])
    .where('status', '=', 'running')
    .orderBy('id', 'desc')
    .top(1)
    .executeTakeFirst();
  return { host: run?.host ?? null, pid: run?.pid ?? null, startedAt: run?.started_at ?? null };
}
