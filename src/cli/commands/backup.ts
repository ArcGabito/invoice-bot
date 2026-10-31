import type { Command } from 'commander';
import { createBackup, listBackups } from '../../infrastructure/database/backup.ts';
import { EXIT_CODES } from '../../shared/errors.ts';
import type { Runner } from '../index.ts';

export function registerBackup(program: Command, run: Runner): void {
  program
    .command('backup')
    .description(
      'Crea una copia verificada de la base de datos (BACKUP DATABASE) y conserva solo las más recientes',
    )
    .option('--list', 'solo lista las copias registradas en SQL Server', false)
    .action((options: { list: boolean }) =>
      run(async (ctx, out) => {
        const { dir, retention } = ctx.config.backup;
        if (!options.list) {
          const result = await createBackup(ctx.db, dir, { retention });
          out.data(result);
          const size = result.size === null ? '' : ` (${Math.round(result.size / 1024)} KB)`;
          out.line(`Copia creada y verificada: ${result.path}${size}`);
          if (result.removed.length) {
            out.line(
              `Copias antiguas eliminadas (se conservan ${retention}): ${result.removed.length}`,
            );
          }
          return EXIT_CODES.OK;
        }
        const backups = await listBackups(ctx.db);
        out.data(backups);
        out.line(`Copias registradas: ${backups.length} (se conservan ${retention})`);
        for (const b of backups) out.line(`  ${b.createdAt.toLocaleString('es-PE')}  ${b.path}`);
        return EXIT_CODES.OK;
      }),
    );
}
