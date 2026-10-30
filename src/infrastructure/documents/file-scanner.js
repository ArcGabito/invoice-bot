import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

export const SUPPORTED_EXTENSIONS = Object.freeze({
  '.xml': 'application/xml',
  '.pdf': 'application/pdf',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
});

/**
 * Descubre archivos compatibles en una carpeta. Devuelve también los no
 * compatibles para poder informarlos. Las rutas se devuelven absolutas y
 * ordenadas para que el resultado sea reproducible.
 */
export async function discoverFiles(root, { recursive = false } = {}) {
  const stat = await fs.promises.stat(root).catch(() => null);
  if (!stat) throw new Error(`No existe la ruta: ${root}`);

  const absoluteRoot = path.resolve(root);
  if (stat.isFile()) return classify([absoluteRoot]);

  const entries = await fs.promises.readdir(absoluteRoot, { recursive, withFileTypes: true });
  const files = entries
    .filter((e) => e.isFile())
    .map((e) => path.join(e.parentPath, e.name))
    .sort((a, b) => a.localeCompare(b));
  return classify(files);
}

function classify(files) {
  const supported = [];
  const unsupported = [];
  for (const filePath of files) {
    const mimeType = SUPPORTED_EXTENSIONS[path.extname(filePath).toLowerCase()];
    if (mimeType) supported.push({ filePath, mimeType });
    else unsupported.push(filePath);
  }
  return { supported, unsupported };
}

/** SHA-256 por streaming: no carga el archivo completo en memoria. */
export function hashFile(filePath) {
  return new Promise((resolve, reject) => {
    const hash = crypto.createHash('sha256');
    let size = 0;
    fs.createReadStream(filePath)
      .on('data', (chunk) => {
        size += chunk.length;
        hash.update(chunk);
      })
      .on('error', reject)
      .on('end', () => resolve({ sha256: hash.digest('hex'), size }));
  });
}
