/**
 * Puerto AiProvider: cualquier proveedor de IA que extraiga los campos de un
 * comprobante. Implementaciones: src/infrastructure/ai/providers/*.
 *
 * @typedef {object} AiExtractionResult
 * @property {import('zod').infer<typeof import('../../infrastructure/ai/extraction-schema.js').ExtractionSchema>} data
 *   Datos que cumplen el esquema de extracción.
 * @property {string} raw     Respuesta textual del modelo, para auditoría.
 * @property {string} model   Modelo que realmente respondió.
 * @property {{ input_tokens: number | null, output_tokens: number | null }} usage
 *
 * @typedef {object} AiProvider
 * @property {string} name
 * @property {string} model
 * @property {(input: { text: string, fileName: string }) => Promise<AiExtractionResult>} extract
 *   Lanza ExtractionError (src/shared/errors.js) cuando falla.
 * @property {() => Promise<string>} ping  Comprueba la conexión sin procesar documentos.
 */
export {};
