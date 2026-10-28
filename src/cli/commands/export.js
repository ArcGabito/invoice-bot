import { exportInvoices } from '../../application/use-cases/export-invoices.js';
import { EXIT_CODES } from '../../shared/errors.js';

export function registerExport(program, run) {
  program
    .command('export')
    .description('Exporta facturas a CSV (por defecto: válidas y con advertencias, sin duplicados)')
    .option('-m, --month <AAAA-MM>', 'periodo de emisión, p. ej. 2026-10')
    .option('--csv', 'formato CSV (único formato disponible por ahora)', true)
    .option(
      '-s, --status <lista>',
      'estados de validación separados por comas: valid,warning,error',
    )
    .option('--include-errors', 'incluye facturas con errores de validación', false)
    .option('--include-duplicates', 'incluye las repeticiones de un mismo comprobante', false)
    .option('--supplier <ruc>', 'solo un proveedor')
    .option('--currency <código>', 'solo una moneda (PEN, USD…)')
    .option('-o, --output <archivo>', 'ruta del CSV (por defecto: exports/invoices-AAAA-MM.csv)')
    .option('--delimiter <carácter>', 'separador de campos', ',')
    .option('--bom', 'añade BOM UTF-8 (recomendado para abrir en Excel)', false)
    .action((options) =>
      run(async (ctx, out) => {
        const result = exportInvoices({
          month: options.month ?? null,
          statuses: options.status ? options.status.split(',').map((s) => s.trim()) : null,
          includeErrors: options.includeErrors,
          includeDuplicates: options.includeDuplicates,
          supplierRuc: options.supplier ?? null,
          currency: options.currency ?? null,
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
