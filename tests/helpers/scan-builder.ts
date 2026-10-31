import fs from 'node:fs';
import { createCanvas } from '@napi-rs/canvas';

/** Generador pseudoaleatorio con semilla: el "ruido" de escaneo es reproducible. */
function seeded(seed) {
  let s = seed >>> 0;
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0;
    return s / 2 ** 32;
  };
}

/**
 * Dibuja las líneas de un comprobante como si fuera una hoja escaneada o una
 * foto: fondo blanco, texto oscuro y, opcionalmente, una leve inclinación y
 * motas de ruido. No contiene texto seleccionable: solo se puede leer con OCR.
 */
export function renderScannedInvoice(
  lines,
  { width = 1240, fontSize = 30, rotateDeg = 0, noise = 0, seed = 7 } = {},
) {
  const lineHeight = Math.round(fontSize * 1.7);
  const height = 160 + lines.length * lineHeight;
  const canvas = createCanvas(width, height);
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = '#ffffff';
  ctx.fillRect(0, 0, width, height);

  ctx.save();
  if (rotateDeg) {
    ctx.translate(width / 2, height / 2);
    ctx.rotate((rotateDeg * Math.PI) / 180);
    ctx.translate(-width / 2, -height / 2);
  }
  ctx.fillStyle = '#1a1a1a';
  ctx.font = `${fontSize}px sans-serif`;
  lines.forEach((line, i) => ctx.fillText(line, 80, 100 + i * lineHeight));
  ctx.restore();

  const random = seeded(seed);
  for (let i = 0; i < noise; i++) {
    const shade = Math.floor(120 + random() * 100);
    ctx.fillStyle = `rgb(${shade},${shade},${shade})`;
    ctx.fillRect(random() * width, random() * height, 1 + random() * 2, 1 + random() * 2);
  }

  return {
    width,
    height,
    png: canvas.toBuffer('image/png'),
    jpeg: canvas.toBuffer('image/jpeg', 85),
  };
}

/**
 * PDF de una página que solo contiene una imagen JPEG, como el que produce un
 * escáner: pdf.js no extrae texto de él.
 */
export function buildImagePdf({ jpeg, width, height }) {
  const pageWidth = 595;
  const pageHeight = Math.round((height / width) * pageWidth);
  const content = Buffer.from(`q ${pageWidth} 0 0 ${pageHeight} 0 0 cm /Im1 Do Q`, 'latin1');
  const parts = [
    '<< /Type /Catalog /Pages 2 0 R >>',
    '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
    `<< /Type /Page /Parent 2 0 R /MediaBox [0 0 ${pageWidth} ${pageHeight}] /Resources << /XObject << /Im1 5 0 R >> >> /Contents 4 0 R >>`,
    { dict: `<< /Length ${content.length} >>`, stream: content },
    {
      dict: `<< /Type /XObject /Subtype /Image /Width ${width} /Height ${height} /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length ${jpeg.length} >>`,
      stream: jpeg,
    },
  ];

  const chunks = [Buffer.from('%PDF-1.4\n%\xe2\xe3\xcf\xd3\n', 'latin1')];
  const offsets: number[] = [];
  let length = chunks[0]!.length;
  parts.forEach((part, i) => {
    offsets.push(length);
    const obj =
      typeof part === 'string'
        ? Buffer.from(`${i + 1} 0 obj\n${part}\nendobj\n`, 'latin1')
        : Buffer.concat([
            Buffer.from(`${i + 1} 0 obj\n${part.dict}\nstream\n`, 'latin1'),
            part.stream,
            Buffer.from('\nendstream\nendobj\n', 'latin1'),
          ]);
    chunks.push(obj);
    length += obj.length;
  });
  const xref = [
    'xref',
    `0 ${parts.length + 1}`,
    '0000000000 65535 f ',
    ...offsets.map((o) => `${String(o).padStart(10, '0')} 00000 n `),
    'trailer',
    `<< /Size ${parts.length + 1} /Root 1 0 R >>`,
    'startxref',
    String(length),
    '%%EOF',
  ].join('\n');
  chunks.push(Buffer.from(`${xref}\n`, 'latin1'));
  return Buffer.concat(chunks);
}

export function writeScannedPdf(filePath, lines, options = {}) {
  fs.writeFileSync(filePath, buildImagePdf(renderScannedInvoice(lines, options)));
  return filePath;
}

export function writeScannedImage(filePath, lines, options = {}) {
  const image = renderScannedInvoice(lines, options);
  fs.writeFileSync(filePath, filePath.toLowerCase().endsWith('.png') ? image.png : image.jpeg);
  return filePath;
}
