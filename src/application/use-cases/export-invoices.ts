import path from 'node:path';
import { VALIDATION_STATUS } from '../../domain/invoice.ts';
import { ConfigError } from '../../shared/errors.ts';
import { writeInvoicesCsv } from '../../infrastructure/exports/csv-exporter.ts';
import { writeInvoicesXlsx } from '../../infrastructure/exports/xlsx-exporter.ts';
import type { Deps } from '../deps.ts';

const MONTH_PATTERN = /^\d{4}-(0[1-9]|1[0-2])$/;
export const EXPORT_FORMATS: readonly string[] = Object.freeze(['csv', 'xlsx']);

/**
 * Caso de uso HU-05: exporta facturas a CSV o Excel (.xlsx).
 * Por defecto incluye solo facturas válidas o con advertencias y excluye las
 * repeticiones de un mismo comprobante (posibles duplicados).
 */
export async function exportInvoices({
  month = null,
  statuses = null,
  includeErrors = false,
  includeDuplicates = false,
  supplierRuc = null,
  currency = null,
  format = 'csv',
  output = null,
  delimiter = ',',
  bom = false,
  deps,
}: {
  month?: string | null;
  statuses?: string[] | null;
  includeErrors?: boolean;
  includeDuplicates?: boolean;
  supplierRuc?: string | null;
  currency?: string | null;
  format?: string;
  output?: string | null;
  delimiter?: string;
  bom?: boolean;
  deps: Pick<Deps, 'repository' | 'config'>;
}): Promise<{ path: string; rows: number }> {
  if (month !== null && !MONTH_PATTERN.test(month)) {
    throw new ConfigError(`El periodo debe tener el formato AAAA-MM (recibido: "${month}")`);
  }
  if (!EXPORT_FORMATS.includes(format)) {
    throw new ConfigError(
      `Formato de exportación desconocido: ${format} (usa ${EXPORT_FORMATS.join(' o ')})`,
    );
  }
  const allowed: string[] = Object.values(VALIDATION_STATUS);
  let validationStatuses: string[] = statuses ?? [
    VALIDATION_STATUS.VALID,
    VALIDATION_STATUS.WARNING,
  ];
  const invalid = validationStatuses.filter((s) => !allowed.includes(s));
  if (invalid.length)
    throw new ConfigError(
      `Estado de validación desconocido: ${invalid.join(', ')} (usa ${allowed.join(', ')})`,
    );
  if (includeErrors && !validationStatuses.includes(VALIDATION_STATUS.ERROR)) {
    validationStatuses = [...validationStatuses, VALIDATION_STATUS.ERROR];
  }

  const rows = await deps.repository.listInvoicesForExport({
    month,
    validationStatuses,
    supplierRuc,
    currency: currency?.toUpperCase() ?? null,
    includeDuplicates,
  });
  const outputPath = path.resolve(
    output ?? path.join(deps.config.exportDir, `invoices-${month ?? 'all'}.${format}`),
  );
  if (format === 'xlsx') {
    const title = `Facturas ${month ?? '(todos los periodos)'} · estados: ${validationStatuses.join(', ')}`;
    return writeInvoicesXlsx(rows, outputPath, { title });
  }
  return writeInvoicesCsv(rows, outputPath, { delimiter, bom });
}
