import { createBackup, listBackups } from '../../infrastructure/database/backup.js';
import { EXIT_CODES } from '../../shared/errors.js';

export function registerBackup(program, run) {
  program
    .command('backup')
    .description('Crea una copia verificada de la base de datos y conserva solo las más recientes')
    .option('--list', 'solo lista las copias existentes', false)
    .action((options) =>
      run(async (ctx, out) => {
        const { dir, retention } = ctx.config.backup;
        if (!options.list) {
          const result = createBackup(ctx.db, dir, { retention });
          out.data(result);
          out.line(
            `Copia creada y verificada: ${result.path} (${Math.round(result.size / 1024)} KB)`,
          );
          if (result.removed.length) {
            out.line(
              `Copias antiguas eliminadas (se conservan ${retention}): ${result.removed.length}`,
            );
          }
          return EXIT_CODES.OK;
        }
        const backups = listBackups(dir);
        out.data(backups);
        out.line(`Copias en ${dir}: ${backups.length} (se conservan ${retention})`);
        for (const b of backups) out.line(`  ${b.createdAt.toLocaleString('es-PE')}  ${b.name}`);
        return EXIT_CODES.OK;
      }),
    );
}
