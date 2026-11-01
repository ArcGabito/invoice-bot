import fs from 'node:fs';
import path from 'node:path';
import { buildMimeMessage, buildZip } from './mail-builder.ts';
import { buildUblInvoice } from './ubl-builder.ts';
import { sampleInvoiceLines, writeTextPdf } from './pdf-builder.ts';
import type { FakeMessage } from './fake-mail-servers.ts';

export const daysAgo = (n: number) => new Date(Date.now() - n * 24 * 60 * 60 * 1000).toISOString();

/** PDF real (con texto) de una factura, como el que adjuntan los emisores. */
export function invoicePdf(dir: string, number: string): Buffer {
  const file = path.join(dir, `tmp-${number}.pdf`);
  writeTextPdf(file, sampleInvoiceLines({ number }));
  const content = fs.readFileSync(file);
  fs.rmSync(file);
  return content;
}

/** Tres correos: XML + PDF sueltos, un ZIP con XML + PDF y uno sin comprobantes. */
export function mailbox(dir: string): FakeMessage[] {
  return [
    {
      id: 'm1',
      receivedAt: daysAgo(3),
      raw: buildMimeMessage({
        subject: 'Factura F001-00000001',
        attachments: [
          {
            name: 'F001-00000001.xml',
            type: 'text/xml',
            content: buildUblInvoice({ id: 'F001-00000001' }),
          },
          {
            name: 'F001-00000001.pdf',
            type: 'application/pdf',
            content: invoicePdf(dir, '00000001'),
          },
        ],
      }),
    },
    {
      id: 'm2',
      receivedAt: daysAgo(2),
      raw: buildMimeMessage({
        subject: 'Comprobante electrónico',
        attachments: [
          {
            name: '20100047218-01-F001-00000002.zip',
            type: 'application/zip',
            content: buildZip({
              '20100047218-01-F001-00000002.xml': buildUblInvoice({ id: 'F001-00000002' }),
              '20100047218-01-F001-00000002.pdf': invoicePdf(dir, '00000002'),
            }),
          },
        ],
      }),
    },
    {
      id: 'm3',
      receivedAt: daysAgo(1),
      raw: buildMimeMessage({
        subject: 'Contrato',
        attachments: [{ name: 'contrato.docx', type: 'application/octet-stream', content: 'x' }],
      }),
    },
  ];
}

/** Nombres de los archivos guardados bajo una carpeta. */
export const savedFiles = (dir: string) =>
  fs.existsSync(dir)
    ? (fs.readdirSync(dir, { recursive: true }) as string[])
        .filter((f) => path.extname(f))
        .map((f) => path.basename(f))
        .sort()
    : [];
