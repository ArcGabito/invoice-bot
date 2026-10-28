import pino from 'pino';

/**
 * Logs estructurados en stderr, para no mezclarlos con la salida de la CLI.
 * Nunca registres el texto ni los datos completos de una factura: usa
 * identificadores (document_id, sha256 abreviado) y metadatos.
 */
export function createLogger({ level = 'warn' } = {}) {
  return pino(
    {
      level,
      base: undefined,
      redact: {
        paths: ['*.apiKey', '*.api_key', '*.text', '*.raw', '*.raw_response'],
        censor: '[redactado]',
      },
    },
    pino.destination(2),
  );
}
