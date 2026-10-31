#!/usr/bin/env node
/**
 * Genera comprobantes ficticios en ./facturas-demo para probar InvoiceBot sin
 * usar facturas reales. Cubre los casos principales del pipeline: válida,
 * con errores, con advertencias, duplicado exacto, posible duplicado,
 * documento sin texto (requiere OCR) y comprobante electrónico XML con su PDF
 * y su constancia de recepción (CDR).
 *
 * Uso: npm run samples [-- <carpeta>]
 */
import fs from 'node:fs';
import path from 'node:path';
import { sampleInvoiceLines, writeTextPdf } from '../tests/helpers/pdf-builder.js';
import { buildCdr, buildUblInvoice, writeUbl } from '../tests/helpers/ubl-builder.js';

const outDir = path.resolve(process.argv[2] ?? 'facturas-demo');
fs.mkdirSync(outDir, { recursive: true });

const samples = {
  '01-factura-valida.pdf': sampleInvoiceLines(),
  '02-factura-total-incorrecto.pdf': sampleInvoiceLines({ number: '00001235', total: '125.00' }),
  '03-factura-ruc-invalido.pdf': sampleInvoiceLines({ ruc: '20100047219', number: '00001236' }),
  '04-factura-igv-raro.pdf': sampleInvoiceLines({
    number: '00001237',
    gravada: '200.00',
    igv: '30.00',
    total: '230.00',
  }),
  '05-reimpresion-de-la-01.pdf': [...sampleInvoiceLines(), 'Representación impresa (copia)'],
  '06-factura-septiembre.pdf': sampleInvoiceLines({ number: '00000980', date: '28/09/2026' }),
  '07-escaneada-sin-texto.pdf': [],
};

for (const [name, lines] of Object.entries(samples)) writeTextPdf(path.join(outDir, name), lines);
fs.copyFileSync(
  path.join(outDir, '01-factura-valida.pdf'),
  path.join(outDir, '08-copia-exacta-de-la-01.pdf'),
);
fs.writeFileSync(
  path.join(outDir, 'notas.txt'),
  'Archivo no compatible: InvoiceBot lo omite y lo informa.\n',
);

// Comprobante electrónico como lo envía un proveedor: XML + PDF con el mismo
// nombre + CDR de SUNAT. Los datos se leen del XML, sin IA.
const stem = '20131312955-01-F002-00000310';
const electronic = {
  id: 'F002-00000310',
  issueDate: '2026-10-20',
  supplierRuc: '20131312955',
  supplierName: 'DISTRIBUIDORA ELECTRÓNICA DEL SUR S.A.C.',
  customerId: '20100047218',
  customerName: 'PROVEEDOR DE EJEMPLO S.A.C.',
  taxes: [
    { scheme: '1000', taxable: '850.00', tax: '153.00' },
    { scheme: '9997', taxable: '120.00', tax: '0.00' },
  ],
  lineExtension: '970.00',
  payable: '1123.00',
};
writeUbl(path.join(outDir, `${stem}.xml`), buildUblInvoice(electronic));
writeTextPdf(path.join(outDir, `${stem}.pdf`), [
  electronic.supplierName,
  `RUC: ${electronic.supplierRuc}`,
  'FACTURA ELECTRÓNICA',
  electronic.id,
  'Representación impresa de la factura electrónica',
]);
writeUbl(path.join(outDir, `R-${stem}.xml`), buildCdr(electronic.id));

// Factura final con un anticipo de S/ 500 ya pagado: el total a pagar lo descuenta.
writeUbl(
  path.join(outDir, '20131312955-01-F002-00000311.xml'),
  buildUblInvoice({
    ...electronic,
    id: 'F002-00000311',
    taxes: [{ scheme: '1000', taxable: '1000.00', tax: '180.00' }],
    lineExtension: '1000.00',
    prepaid: '500.00',
    payable: '680.00',
  }),
);

console.log(`Comprobantes de prueba creados en ${outDir}`);
