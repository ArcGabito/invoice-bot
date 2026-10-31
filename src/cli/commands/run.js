import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { InvalidArgumentError } from 'commander';
import { runBot } from '../../application/use-cases/run-bot.js';
import { createBackup, isBackupDue } from '../../infrastructure/database/backup.js';
import { LockedError, acquireLock } from '../../infrastructure/runtime/lock.js';
import { ConfigError, EXIT_CODES } from '../../shared/errors.js';

const TRIGGERS = ['manual', 'scheduled'];

function parseInterval(value) {
  const n = Number(value);
  if (!Number.isInteger(n) || n < 1)
    throw new InvalidArgumentError('debe ser un entero de minutos');
  return n;
}

export function registerRun(program, run) {
  program
    .command('run')
    .description(
      'Ejecución desatendida: reintenta, escanea la carpeta de entrada, verifica, exporta y respalda',
    )
    .option('--inbox <ruta>', 'carpeta de entrada (por defecto: INVOICEBOT_INBOX_DIR)')
    .option('--watch', 'repite el ciclo cada --interval minutos hasta detenerlo (Ctrl+C)', false)
    .option('--interval <minutos>', 'minutos entre ciclos con --watch', parseInterval)
    .option('--trigger <origen>', `origen de la ejecución: ${TRIGGERS.join(' o ')}`, 'manual')
    .action((options) =>
      run(async (ctx, out) => {
        const { config } = ctx;
        const inboxDir = options.inbox ? path.resolve(options.inbox) : config.run.inboxDir;
        if (!inboxDir) {
          throw new ConfigError(
            'Define la carpeta de entrada con INVOICEBOT_INBOX_DIR o --inbox <ruta>',
          );
        }
        if (!fs.existsSync(inboxDir))
          throw new ConfigError(`No existe la carpeta de entrada: ${inboxDir}`);
        if (!TRIGGERS.includes(options.trigger)) {
          throw new ConfigError(`--trigger debe ser ${TRIGGERS.join(' o ')}`);
        }

        let release;
        try {
          release = acquireLock(path.join(config.dataDir, 'invoicebot.lock'));
        } catch (error) {
          if (!(error instanceof LockedError)) throw error;
          // Con una tarea programada es normal que un ciclo largo se solape con el siguiente.
          out.data({ skipped: true, reason: error.message });
          out.line(`${error.message}. Se omite este ciclo.`);
          ctx.logger.warn({ holder: error.holder }, 'ciclo omitido: ya hay una ejecución en curso');
          return EXIT_CODES.OK;
        }

        try {
          const abandoned = ctx.repository.closeAbandonedBotRuns();
          if (abandoned)
            ctx.logger.warn({ abandoned }, 'ejecuciones anteriores cerradas como fallidas');
          // Si la verificación automática está activada, se valida su configuración antes de empezar.
          const sunatClient =
            config.sunat.enabled && config.sunat.verifyOnScan ? ctx.sunatClient : null;
          const backup = {
            isDue: () => isBackupDue(config.backup.dir, config.backup.intervalHours),
            create: () =>
              createBackup(ctx.db, config.backup.dir, { retention: config.backup.retention }),
          };
          const cycle = (trigger) =>
            runBot({
              deps: ctx,
              inboxDir,
              recursive: config.run.recursive,
              trigger,
              sunatClient,
              exportFormat: config.run.export,
              backup,
              host: os.hostname(),
              pid: process.pid,
            });

          if (!options.watch) {
            const result = await cycle(options.trigger);
            out.data(result);
            printCycle(out, result);
            return result.exitCode;
          }
          return await watch(out, cycle, options.interval ?? config.run.intervalMinutes);
        } finally {
          release();
        }
      }),
    );
}

async function watch(out, cycle, intervalMinutes) {
  let stopping = false;
  let wake = () => {};
  const stop = () => {
    stopping = true;
    wake();
  };
  process.once('SIGINT', stop);
  process.once('SIGTERM', stop);
  out.line(`Modo vigilancia: un ciclo cada ${intervalMinutes} min. Ctrl+C para detener.`);
  try {
    while (!stopping) {
      const result = await cycle('watch');
      out.data(result);
      printCycle(out, result);
      if (stopping) break;
      await new Promise((resolve) => {
        wake = resolve;
        setTimeout(resolve, intervalMinutes * 60 * 1000).unref();
      });
    }
  } finally {
    process.off('SIGINT', stop);
    process.off('SIGTERM', stop);
  }
  out.line('Vigilancia detenida.');
  return EXIT_CODES.OK;
}

const STATUS_LABEL = { success: 'correcta', partial: 'parcial', failed: 'fallida' };

function printCycle(out, { runId, status, report, error }) {
  const s = report.scan;
  out.line(`Ejecución #${runId} ${STATUS_LABEL[status]} · ${new Date().toLocaleString('es-PE')}`);
  if (error) {
    out.line(`  Error: ${error}`);
    return;
  }
  if (report.retry.queued) {
    out.line(`  Reintentados: ${report.retry.queued} (fallidos de nuevo: ${report.retry.failed})`);
  }
  out.line(
    `  Documentos: ${s.found} encontrados · ${s.extracted} nuevos (válidos ${s.valid}, advertencias ${s.warning}, errores ${s.error}) · ${s.failed} fallidos · ${s.needsOcr} requieren OCR`,
  );
  if (report.sunat) {
    out.line(
      report.sunat.error
        ? `  SUNAT: no se completó (${report.sunat.error})`
        : `  SUNAT: ${report.sunat.checked} consultados · ${report.sunat.voided} anulados · ${report.sunat.notFound} no informados`,
    );
  }
  for (const e of report.exports) out.line(`  Exportado: ${e.path} (${e.rows} facturas)`);
  if (report.backup) out.line(`  Copia de seguridad: ${report.backup.path}`);
}
