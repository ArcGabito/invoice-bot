import fs from 'node:fs';
import path from 'node:path';

/** Acceso guardado por `invoicebot mail login`: el refresh token del usuario. */
export interface StoredMailToken {
  provider: 'gmail' | 'graph';
  clientId: string;
  account: string;
  refreshToken: string;
  savedAt: string;
}

/**
 * Archivo con el refresh token del correo (data/mail-token.json). Solo lo
 * puede leer el usuario que ejecuta el bot. Microsoft entrega un token nuevo
 * en cada renovación: se reescribe para que el acceso no venza.
 */
export function readMailToken(file: string): StoredMailToken | null {
  try {
    const token = JSON.parse(fs.readFileSync(file, 'utf8')) as StoredMailToken;
    return token.refreshToken ? token : null;
  } catch {
    return null;
  }
}

export function writeMailToken(file: string, token: Omit<StoredMailToken, 'savedAt'>): void {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const tmp = `${file}.tmp`;
  fs.writeFileSync(
    tmp,
    `${JSON.stringify({ ...token, savedAt: new Date().toISOString() }, null, 2)}\n`,
    {
      mode: 0o600,
    },
  );
  fs.renameSync(tmp, file);
}
