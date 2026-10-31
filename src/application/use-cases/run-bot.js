import { exportInvoices } from './export-invoices.js';
import { retryDocuments } from './retry-failed.js';
import { scanInvoices } from './scan-invoices.js';
import { hasSunatProblems, verifySunat } from './verify-sunat.js';

/** "2026-10-15" → ["2026-10", "2026-09"]: mes actual y anterior. */
export function currentAndPreviousMonth(today) {
  const [y, m] = today.split('-').map(Number);
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
 *
 * @returns {Promise<{ runId: number, status: 'success' | 'partial' | 'failed', exitCode: number, report: object, error?: string }>}
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
}) {
  const { repository, logger, today } = deps;
  const runId = repository.startBotRun({ trigger, host, pid });
  const log = logger.child({ bot_run: runId });
  log.info({ trigger, inboxDir }, 'inicio de ejecución');

  const report = { inboxDir, retry: null, scan: null, sunat: null, exports: [], backup: null };
  let status = 'success';
  let exitCode = 0;
  let error = null;
  try {
    report.retry = await retryDocuments({ deps, failed: true, pending: false });
    report.scan = await scanInvoices({ root: inboxDir, recursive, deps });

    if (sunatClient) {
      try {
        report.sunat = await verifySunat({ deps, sunatClient });
      } catch (sunatError) {
        report.sunat = { error: sunatError.message };
      }
    }
    if (exportFormat !== 'none') {
      for (const month of currentAndPreviousMonth(today)) {
        report.exports.push(await exportInvoices({ month, format: exportFormat, deps }));
      }
    }
    if (backup?.isDue()) report.backup = backup.create();

    const problems =
      report.scan.failed > 0 ||
      report.scan.error > 0 ||
      report.retry.failed > 0 ||
      (report.sunat !== null && (Boolean(report.sunat.error) || hasSunatProblems(report.sunat)));
    if (problems) {
      status = 'partial';
      exitCode = 3;
    }
  } catch (cycleError) {
    status = 'failed';
    exitCode = cycleError.exitCode ?? 1;
    error = cycleError.message;
    log.error({ err: { message: cycleError.message, code: cycleError.code } }, 'ejecución fallida');
  } finally {
    repository.finishBotRun(runId, {
      status,
      exitCode,
      summary: compactReport(report),
      errorMessage: error,
    });
  }
  log.info({ status, exitCode }, 'fin de ejecución');
  return { runId, status, exitCode, report, ...(error && { error }) };
}

/** Lo esencial de un ciclo, para guardar en bot_runs sin listas largas. */
export function compactReport(report) {
  const s = report.scan;
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
    sunatChecked: report.sunat?.checked ?? null,
    sunatError: report.sunat?.error ?? null,
    exports: report.exports.map((e) => e.path),
    backup: report.backup?.path ?? null,
  };
}
