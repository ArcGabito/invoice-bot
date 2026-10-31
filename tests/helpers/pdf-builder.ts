import fs from 'node:fs';

/**
 * Genera un PDF mínimo y válido con una línea de texto por elemento de `lines`
 * (fuente Helvetica, codificación WinAnsi). Permite probar la extracción de
 * texto sin guardar facturas reales en el repositorio.
 */
export function buildTextPdf(lines: readonly string[]): Buffer {
  const escape = (s: string) =>
    s.replace(/\\/g, '\\\\').replace(/\(/g, '\\(').replace(/\)/g, '\\)');
  const content = [
    'BT',
    '/F1 10 Tf',
    '14 TL',
    '50 780 Td',
    ...lines.map((l: string) => `(${escape(l)}) Tj T*`),
    'ET',
  ].join('\n');
  const contentBytes = Buffer.from(content, 'latin1');

  const objects = [
    '<< /Type /Catalog /Pages 2 0 R >>',
    '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>',
    null, // contenido
    '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>',
  ];

  const chunks = [Buffer.from('%PDF-1.4\n%\xe2\xe3\xcf\xd3\n', 'latin1')];
  const offsets: number[] = [];
  let length = chunks[0]!.length;
  objects.forEach((body, i) => {
    offsets.push(length);
    const obj =
      body === null
        ? Buffer.concat([
            Buffer.from(`${i + 1} 0 obj\n<< /Length ${contentBytes.length} >>\nstream\n`, 'latin1'),
            contentBytes,
            Buffer.from('\nendstream\nendobj\n', 'latin1'),
          ])
        : Buffer.from(`${i + 1} 0 obj\n${body}\nendobj\n`, 'latin1');
    chunks.push(obj);
    length += obj.length;
  });

  const xref = [
    'xref',
    `0 ${objects.length + 1}`,
    '0000000000 65535 f ',
    ...offsets.map((o) => `${String(o).padStart(10, '0')} 00000 n `),
    'trailer',
    `<< /Size ${objects.length + 1} /Root 1 0 R >>`,
    'startxref',
    String(length),
    '%%EOF',
  ].join('\n');
  chunks.push(Buffer.from(`${xref}\n`, 'latin1'));
  return Buffer.concat(chunks);
}

export function writeTextPdf(filePath: string, lines: readonly string[]): string {
  fs.writeFileSync(filePath, buildTextPdf(lines));
  return filePath;
}

/** Líneas de una factura ficticia y aritméticamente coherente. */
export function sampleInvoiceLines({
  ruc = '20100047218',
  series = 'F001',
  number = '00001234',
  date = '15/10/2026',
  gravada = '100.00',
  igv = '18.00',
  total = '118.00',
} = {}) {
  return [
    'PROVEEDOR DE EJEMPLO S.A.C.',
    'Av. Siempre Viva 123 - Lima',
    `RUC: ${ruc}`,
    'FACTURA ELECTRÓNICA',
    `${series}-${number}`,
    `Fecha de emisión: ${date}`,
    'Señor(es): CLIENTE DE PRUEBA E.I.R.L.',
    'RUC: 20131312955',
    'Moneda: SOLES',
    'Descripción: Servicio de consultoría',
    `Op. Gravada: S/ ${gravada}`,
    `IGV 18%: S/ ${igv}`,
    `Importe Total: S/ ${total}`,
  ];
}
