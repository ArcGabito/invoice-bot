import path from 'node:path';
import { VALIDATION_STATUS } from '../../domain/invoice.js';
import { ConfigError } from '../../shared/errors.js';
import { writeInvoicesCsv } from '../../infrastructure/exports/csv-exporter.js';

const MONTH_PATTERN = /^\d{4}-(0[1-9]|1[0-2])$/;

/**
 * Caso de uso HU-05: exporta facturas a CSV.
 * Por defecto incluye solo facturas válidas o con advertencias y excluye las
 * repeticiones de un mismo comprobante (posibles duplicados).
 */
export function exportInvoices({
  month = null,
  statuses = null,
  includeErrors = false,
  includeDuplicates = false,
  supplierRuc = null,
  currency = null,
  output = null,
  delimiter = ',',
  bom = false,
  deps,
}) {
  if (month !== null && !MONTH_PATTERN.test(month)) {
    throw new ConfigError(`El periodo debe tener el formato AAAA-MM (recibido: "${month}")`);
  }
  const allowed = Object.values(VALIDATION_STATUS);
  let validationStatuses = statuses ?? [VALIDATION_STATUS.VALID, VALIDATION_STATUS.WARNING];
  const invalid = validationStatuses.filter((s) => !allowed.includes(s));
  if (invalid.length)
    throw new ConfigError(
      `Estado de validación desconocido: ${invalid.join(', ')} (usa ${allowed.join(', ')})`,
    );
  if (includeErrors && !validationStatuses.includes(VALIDATION_STATUS.ERROR)) {
    validationStatuses = [...validationStatuses, VALIDATION_STATUS.ERROR];
  }

  const rows = deps.repository.listInvoicesForExport({
    month,
    validationStatuses,
    supplierRuc,
    currency: currency?.toUpperCase() ?? null,
    includeDuplicates,
  });
  const outputPath = path.resolve(
    output ?? path.join(deps.config.exportDir, `invoices-${month ?? 'all'}.csv`),
  );
  return writeInvoicesCsv(rows, outputPath, { delimiter, bom });
}
