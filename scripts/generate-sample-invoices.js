#!/usr/bin/env node
/**
 * Genera comprobantes ficticios en ./facturas-demo para probar InvoiceBot sin
 * usar facturas reales. Cubre los casos principales del pipeline: válida,
 * con errores, con advertencias, duplicado exacto, posible duplicado y
 * documento sin texto (requiere OCR).
 *
 * Uso: npm run samples [-- <carpeta>]
 */
import fs from 'node:fs';
import path from 'node:path';
import { sampleInvoiceLines, writeTextPdf } from '../tests/helpers/pdf-builder.js';

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

console.log(`Comprobantes de prueba creados en ${outDir}`);
