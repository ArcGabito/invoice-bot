import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { InvoiceBotError } from '../../shared/errors.js';

export class LockedError extends InvoiceBotError {
  constructor(holder) {
    super(
      `Ya hay una ejecución en curso (PID ${holder.pid} en ${holder.host}, desde ${holder.startedAt})`,
      { code: 'LOCKED' },
    );
    this.holder = holder;
  }
}

function isAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    // EPERM: el proceso existe pero pertenece a otro usuario.
    return error.code === 'EPERM';
  }
}

/**
 * Candado de un solo proceso basado en archivo (creación exclusiva). Evita
 * que dos ejecuciones programadas procesen los mismos documentos a la vez.
 * Un candado se considera abandonado si su proceso ya no existe en este
 * equipo o si supera `staleMs` (un proceso colgado no bloquea para siempre).
 *
 * @returns {() => void} función que libera el candado
 */
export function acquireLock(lockPath, { staleMs = 6 * 3600 * 1000, now = () => Date.now() } = {}) {
  fs.mkdirSync(path.dirname(lockPath), { recursive: true });
  const info = {
    pid: process.pid,
    host: os.hostname(),
    startedAt: new Date(now()).toISOString(),
  };

  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      fs.writeFileSync(lockPath, JSON.stringify(info), { flag: 'wx' });
      let released = false;
      return () => {
        if (released) return;
        released = true;
        fs.rmSync(lockPath, { force: true });
      };
    } catch (error) {
      if (error.code !== 'EEXIST') throw error;
      let holder;
      try {
        holder = JSON.parse(fs.readFileSync(lockPath, 'utf8'));
      } catch {
        holder = null; // archivo corrupto o a medio escribir: se trata como abandonado
      }
      const sameHost = holder?.host === info.host;
      const stale =
        !holder ||
        now() - Date.parse(holder.startedAt) > staleMs ||
        (sameHost && !isAlive(holder.pid));
      if (!stale) throw new LockedError(holder);
      fs.rmSync(lockPath, { force: true });
    }
  }
  throw new Error(`No se pudo obtener el candado ${lockPath}`);
}
