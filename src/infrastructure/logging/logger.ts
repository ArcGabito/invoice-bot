import fs from 'node:fs';
import path from 'node:path';
import pino from 'pino';
import type { Level, LevelWithSilent, Logger, LoggerOptions, StreamEntry } from 'pino';

export type { Logger };

const LOG_FILE_PATTERN = /^invoicebot-(\d{4}-\d{2}-\d{2})\.log$/;

const localDate = (date = new Date()) => {
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
};

/**
 * Archivo de log diario (invoicebot-AAAA-MM-DD.log). Cambia de archivo al
 * cambiar el día, también en un proceso que corre varios días (`run --watch`).
 * Escribe de forma síncrona: si el proceso se cae, la última línea ya está en disco.
 */
export function createDailyFileStream(
  dir: string,
  { now = () => new Date() }: { now?: () => Date } = {},
) {
  fs.mkdirSync(dir, { recursive: true });
  return {
    write(line: string) {
      fs.appendFileSync(path.join(dir, `invoicebot-${localDate(now())}.log`), line);
    },
  };
}

/** Borra los archivos de log con más de `retentionDays` días. Devuelve los borrados. */
export function pruneLogFiles(dir: string, retentionDays: number, now = new Date()): string[] {
  if (!fs.existsSync(dir)) return [];
  const limit = new Date(now);
  limit.setDate(limit.getDate() - retentionDays);
  const cutoff = localDate(limit);
  const removed: string[] = [];
  for (const name of fs.readdirSync(dir)) {
    const day = LOG_FILE_PATTERN.exec(name)?.[1];
    if (day && day < cutoff) {
      fs.rmSync(path.join(dir, name), { force: true });
      removed.push(name);
    }
  }
  return removed;
}

/**
 * Logs estructurados (JSON por línea). En pantalla van a stderr, para no
 * mezclarlos con la salida de la CLI; si se configura `fileDir`, también a un
 * archivo diario con nivel al menos "info", para poder auditar ejecuciones
 * desatendidas. Nunca registres el texto ni los datos completos de una
 * factura: usa identificadores (document_id, sha256 abreviado) y metadatos.
 */
export function createLogger({
  level = 'warn',
  fileDir = null,
  retentionDays = 30,
}: { level?: LevelWithSilent; fileDir?: string | null; retentionDays?: number } = {}): Logger {
  const options: LoggerOptions = {
    base: undefined,
    timestamp: pino.stdTimeFunctions.isoTime,
    redact: {
      paths: ['*.apiKey', '*.api_key', '*.text', '*.raw', '*.raw_response', '*.client_secret'],
      censor: '[redactado]',
    },
  };
  if (!fileDir) return pino({ ...options, level }, pino.destination(2));

  pruneLogFiles(fileDir, retentionDays);
  const rank = (l: string) => pino.levels.values[l] ?? Infinity;
  const fileLevel: Level = level !== 'silent' && rank(level) < rank('info') ? level : 'info';
  const streams: (StreamEntry & { level: Level })[] = [
    { level: fileLevel, stream: createDailyFileStream(fileDir) },
  ];
  if (level !== 'silent') streams.push({ level, stream: pino.destination(2) });
  const minLevel = streams.reduce((min, s) => (rank(s.level) < rank(min.level) ? s : min)).level;
  return pino({ ...options, level: minLevel }, pino.multistream(streams));
}
