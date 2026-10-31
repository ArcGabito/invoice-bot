import fs from 'node:fs';
import { sql } from 'kysely';
import type { Db } from './connection.ts';

/**
 * Copias de seguridad con BACKUP DATABASE. El archivo lo escribe el servicio
 * de SQL Server, así que `dir` es una ruta del servidor. Para borrar las copias
 * antiguas, InvoiceBot necesita ver esa misma carpeta en la misma ruta (en
 * docker-compose, un volumen montado igual en los dos contenedores); si no la
 * ve, la retención la gestiona TI.
 *
 * Copias periódicas: <base>-AAAAMMDD-HHMMSS.bak (invoicebot-… con el nombre
 * por defecto). El nombre de la base evita que dos bases de la misma
 * instancia escriban el mismo archivo. Las previas a una migración
 * (<base>-vN-…) no se borran.
 */
const BACKUP_PATTERN = /^[A-Za-z][A-Za-z0-9_]*-(\d{8})-(\d{6})\.bak$/;

export interface BackupInfo {
  name: string;
  path: string;
  createdAt: Date;
  size: number | null;
}

function stamp(date: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0');
  return (
    `${date.getUTCFullYear()}${pad(date.getUTCMonth() + 1)}${pad(date.getUTCDate())}-` +
    `${pad(date.getUTCHours())}${pad(date.getUTCMinutes())}${pad(date.getUTCSeconds())}`
  );
}

/** Une la carpeta y el nombre con el separador del servidor (\ en Windows, / en Linux). */
function serverPath(dir: string, name: string): string {
  const sep = dir.includes('\\') && !dir.includes('/') ? '\\' : '/';
  return dir.replace(/[\\/]+$/, '') + sep + name;
}

/**
 * Copias periódicas registradas por SQL Server para esta base de datos, de la
 * más reciente a la más antigua (historial de msdb). Solo cuenta las de la
 * base actual: si se borró y se volvió a crear con el mismo nombre, las
 * copias anteriores no son suyas. msdb guarda la hora local del servidor; se
 * pasa a UTC.
 */
export async function listBackups(db: Db, limit = 50): Promise<BackupInfo[]> {
  const result = await sql<{ path: string; finished: Date | string; size: number | string | null }>`
    SELECT TOP (${limit}) f.physical_device_name AS path,
      DATEADD(MINUTE, DATEDIFF(MINUTE, SYSDATETIME(), SYSUTCDATETIME()), s.backup_finish_date) AS finished,
      s.backup_size AS size
    FROM msdb.dbo.backupset s
    JOIN msdb.dbo.backupmediafamily f ON f.media_set_id = s.media_set_id
    WHERE s.database_name = DB_NAME() AND s.type = 'D'
      -- msdb guarda la fecha de creación sin milisegundos; sys.databases, con ellos.
      AND ABS(DATEDIFF(SECOND, s.database_creation_date,
        (SELECT create_date FROM sys.databases WHERE database_id = DB_ID()))) <= 1
    ORDER BY s.backup_finish_date DESC`.execute(db);
  // Dos copias en el mismo segundo comparten archivo (la segunda lo reemplaza): se cuenta una vez.
  const seen = new Set<string>();
  return result.rows
    .filter((r) => !seen.has(r.path) && seen.add(r.path))
    .map((r) => ({
      name: r.path.split(/[\\/]/).pop() ?? r.path,
      path: r.path,
      createdAt: new Date(r.finished),
      size: r.size === null ? null : Number(r.size),
    }))
    .filter((b) => BACKUP_PATTERN.test(b.name));
}

export async function isBackupDue(
  db: Db,
  intervalHours: number,
  now = new Date(),
): Promise<boolean> {
  const [latest] = await listBackups(db, 1);
  return !latest || now.getTime() - latest.createdAt.getTime() >= intervalHours * 3600 * 1000;
}

/** Carpeta de copias por defecto de la instancia (la que configuró TI al instalar SQL Server). */
export async function defaultBackupDir(db: Db): Promise<string> {
  const result = await sql<{ dir: string | null }>`
    SELECT CAST(SERVERPROPERTY('InstanceDefaultBackupPath') AS NVARCHAR(4000)) AS dir`.execute(db);
  const dir = result.rows[0]?.dir;
  if (!dir)
    throw new Error(
      'SQL Server no informa una carpeta de copias por defecto: define INVOICEBOT_BACKUP_DIR',
    );
  return dir;
}

/**
 * Copia completa con suma de comprobación, verificada con RESTORE VERIFYONLY.
 * `dir` null usa la carpeta de copias por defecto de la instancia. Después
 * borra las copias periódicas que excedan `retention`, si la carpeta es
 * accesible desde aquí.
 */
export async function createBackup(
  db: Db,
  dir: string | null,
  {
    retention = null,
    label = null,
    now = new Date(),
  }: { retention?: number | null; label?: string | null; now?: Date } = {},
): Promise<{ path: string; size: number | null; removed: string[] }> {
  const current = await sql<{ name: string }>`SELECT DB_NAME() AS name`.execute(db);
  const base = current.rows[0]?.name ?? 'invoicebot';
  const name = label ? `${base}-${label}-${stamp(now)}.bak` : `${base}-${stamp(now)}.bak`;
  const target = serverPath(dir ?? (await defaultBackupDir(db)), name);
  // COPY_ONLY: no altera la cadena de copias que TI pueda tener programada.
  await sql`
    DECLARE @db sysname = DB_NAME();
    BACKUP DATABASE @db TO DISK = ${target} WITH COPY_ONLY, CHECKSUM, INIT, NAME = N'InvoiceBot'`.execute(
    db,
  );
  await sql`RESTORE VERIFYONLY FROM DISK = ${target} WITH CHECKSUM`.execute(db);

  const removed: string[] = [];
  if (retention !== null && !label) {
    for (const old of (await listBackups(db, 500)).slice(retention)) {
      if (fs.existsSync(old.path)) {
        fs.rmSync(old.path, { force: true });
        removed.push(old.name);
      }
    }
  }
  const local = fs.existsSync(target) ? fs.statSync(target).size : null;
  return { path: target, size: local, removed };
}
