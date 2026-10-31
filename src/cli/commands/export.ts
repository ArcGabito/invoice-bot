import { exportInvoices } from '../../application/use-cases/export-invoices.ts';
import { EXIT_CODES } from '../../shared/errors.ts';
import type { Command } from 'commander';
import type { Runner } from '../index.ts';

interface ExportOptions {
  month?: string;
  format?: string;
  csv?: boolean;
  xlsx?: boolean;
  status?: string;
  includeErrors: boolean;
  includeDuplicates: boolean;
  supplier?: string;
  currency?: string;
  output?: string;
  delimiter: string;
  bom: boolean;
}

export function registerExport(program: Command, run: Runner): void {
  program
    .command('export')
    .description(
      'Exporta facturas a CSV o Excel (por defecto: válidas y con advertencias, sin duplicados)',
    )
    .option('-m, --month <AAAA-MM>', 'periodo de emisión, p. ej. 2026-10')
    .option('-f, --format <formato>', 'csv o xlsx (por defecto: INVOICEBOT_EXPORT_FORMAT, o csv)')
    .option('--csv', 'atajo de --format csv')
    .option('--xlsx', 'atajo de --format xlsx')
    .option(
      '-s, --status <lista>',
      'estados de validación separados por comas: valid,warning,error',
    )
    .option('--include-errors', 'incluye facturas con errores de validación', false)
    .option('--include-duplicates', 'incluye las repeticiones de un mismo comprobante', false)
    .option('--supplier <ruc>', 'solo un proveedor')
    .option('--currency <código>', 'solo una moneda (PEN, USD…)')
    .option(
      '-o, --output <archivo>',
      'ruta del archivo (por defecto: exports/invoices-AAAA-MM.<formato>)',
    )
    .option('--delimiter <carácter>', 'separador de campos del CSV', ',')
    .option('--bom', 'añade BOM UTF-8 al CSV (recomendado para abrirlo en Excel)', false)
    .action((options: ExportOptions) =>
      run(async (ctx, out) => {
        const format =
          options.format ?? (options.xlsx ? 'xlsx' : options.csv ? 'csv' : ctx.config.exportFormat);
        const result = await exportInvoices({
          month: options.month ?? null,
          statuses: options.status ? options.status.split(',').map((s: string) => s.trim()) : null,
          includeErrors: options.includeErrors,
          includeDuplicates: options.includeDuplicates,
          supplierRuc: options.supplier ?? null,
          currency: options.currency ?? null,
          format: format.toLowerCase(),
          output: options.output ?? null,
          delimiter: options.delimiter === '\\t' ? '\t' : options.delimiter,
          bom: options.bom,
          deps: ctx,
        });
        out.data(result);
        out.line(`Exportadas ${result.rows} facturas a ${result.path}`);
        return EXIT_CODES.OK;
      }),
    );
}
