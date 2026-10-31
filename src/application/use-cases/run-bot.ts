import { exportInvoices } from './export-invoices.ts';
import { retryDocuments } from './retry-failed.ts';
import { scanInvoices } from './scan-invoices.ts';
import { hasSunatProblems, verifySunat } from './verify-sunat.ts';
import type { SunatSummary } from './verify-sunat.ts';
import type { RetrySummary } from './retry-failed.ts';
import type { ScanDeps, ScanSummary } from './scan-invoices.ts';
import type { SunatClient } from '../../infrastructure/sunat/sunat-client.ts';
import type { Deps } from '../deps.ts';
import { errorMessage } from '../../shared/errors.ts';

export interface BotReport {
  inboxDir: string;
  retry: RetrySummary | null;
  scan: ScanSummary | null;
  sunat: SunatSummary | { error: string } | null;
  exports: { path: string; rows: number }[];
  backup: { path: string } | null;
}

export type BotRunStatus = 'success' | 'partial' | 'failed';

/** Copia de seguridad del ciclo: `run` la hace si la última es más antigua que el intervalo. */
export interface BackupStep {
  isDue(): Promise<boolean>;
  create(): Promise<{ path: string }>;
}

/** "2026-10-15" → ["2026-10", "2026-09"]: mes actual y anterior. */
export function currentAndPreviousMonth(today: string): [string, string] {
  const [y = 0, m = 0] = today.split('-').map(Number);
  const prev = m === 1 ? `${y - 1}-12` : `${y}-${String(m - 1).padStart(2, '0')}`;
  return [today.slice(0, 7), prev];
}

/**
 * Un ciclo completo del bot desatendido:
 * 1. Reintenta los documentos que fallaron en ejecuciones anteriores
 *    (errores temporales, dentro del límite de intentos).
 * 2. Escanea la carpeta de entrada.
 * 3. Verifica en SUNAT, si está activado.
 * 4. Exporta el mes actual y el anterior, si está configurado (las facturas
 *    de fin de mes llegan durante los primeros días del siguiente).
 * 5. Hace una copia de seguridad si la última es más antigua que el intervalo.
 *
 * Cada ciclo queda registrado en bot_runs. Un fallo en la verificación de
 * SUNAT o en un documento no detiene el ciclo: lo marca como parcial.
 */
export async function runBot({
  deps,
  inboxDir,
  recursive = true,
  trigger = 'manual',
  sunatClient = null,
  exportFormat = 'none',
  backup = null,
  host = null,
  pid = null,
}: {
  deps: ScanDeps & Pick<Deps, 'config'>;
  inboxDir: string;
  recursive?: boolean;
  trigger?: string;
  sunatClient?: Pick<SunatClient, 'validateReceipt'> | null;
  exportFormat?: string;
  backup?: BackupStep | null;
  host?: string | null;
  pid?: number | null;
}): Promise<{
  runId: number;
  status: BotRunStatus;
  exitCode: number;
  report: BotReport;
  error?: string;
}> {
  const { repository, logger, today } = deps;
  const runId = await repository.startBotRun({ trigger, host, pid });
  const log = logger.child({ bot_run: runId });
  log.info({ trigger, inboxDir }, 'inicio de ejecución');

  const report: BotReport = {
    inboxDir,
    retry: null,
    scan: null,
    sunat: null,
    exports: [],
    backup: null,
  };
  let status: BotRunStatus = 'success';
  let exitCode = 0;
  let error: string | null = null;
  try {
    const retry = await retryDocuments({ deps, failed: true, pending: false });
    report.retry = retry;
    const scan = await scanInvoices({ root: inboxDir, recursive, deps });
    report.scan = scan;

    if (sunatClient) {
      try {
        report.sunat = await verifySunat({ deps, sunatClient });
      } catch (sunatError) {
        report.sunat = { error: errorMessage(sunatError) };
      }
    }
    if (exportFormat !== 'none') {
      for (const month of currentAndPreviousMonth(today)) {
        report.exports.push(await exportInvoices({ month, format: exportFormat, deps }));
      }
    }
    if (backup && (await backup.isDue())) report.backup = await backup.create();

    const sunat = report.sunat;
    const problems =
      scan.failed > 0 ||
      scan.error > 0 ||
      retry.failed > 0 ||
      (sunat !== null && ('error' in sunat || hasSunatProblems(sunat)));
    if (problems) {
      status = 'partial';
      exitCode = 3;
    }
  } catch (cycleError) {
    const failure = cycleError as { exitCode?: number; code?: string };
    status = 'failed';
    exitCode = failure.exitCode ?? 1;
    error = errorMessage(cycleError);
    log.error({ err: { message: error, code: failure.code } }, 'ejecución fallida');
  } finally {
    await repository.finishBotRun(runId, {
      status,
      exitCode,
      summary: compactReport(report),
      errorMessage: error,
    });
  }
  log.info({ status, exitCode }, 'fin de ejecución');
  return { runId, status, exitCode, report, ...(error !== null && { error }) };
}

/** Lo esencial de un ciclo, para guardar en bot_runs sin listas largas. */
export function compactReport(report: BotReport) {
  const s = report.scan;
  const sunat = report.sunat;
  return {
    found: s?.found ?? 0,
    extracted: s?.extracted ?? 0,
    valid: s?.valid ?? 0,
    warning: s?.warning ?? 0,
    error: s?.error ?? 0,
    failed: s?.failed ?? 0,
    needsOcr: s?.needsOcr ?? 0,
    fromXml: s?.fromXml ?? 0,
    retried: report.retry?.queued ?? 0,
    retryFailed: report.retry?.failed ?? 0,
    sunatChecked: sunat && 'checked' in sunat ? sunat.checked : null,
    sunatError: sunat && 'error' in sunat ? sunat.error : null,
    exports: report.exports.map((e) => e.path),
    backup: report.backup?.path ?? null,
  };
}
