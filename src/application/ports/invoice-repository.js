/**
 * Puerto InvoiceRepository: persistencia de documentos, extracciones,
 * facturas e incidencias. Implementación:
 * src/infrastructure/database/repositories/sqlite-invoice-repository.js.
 *
 * Los casos de uso solo dependen de estos métodos; una implementación en
 * memoria o con otro motor debe respetar las mismas garantías:
 * - `saveInvoiceResult` es atómico (factura + incidencias + estado del documento).
 * - `insertDocument` rechaza un SHA-256 ya registrado.
 *
 * @typedef {object} InvoiceRepository
 * @property {(sha256: string) => object | null} findDocumentBySha
 * @property {(id: number) => object | null} getDocument
 * @property {(doc: { sha256: string, file_path: string, file_name: string, mime_type: string, file_size: number }) => object} insertDocument
 * @property {(documentId: number, filePath: string) => boolean} recordOccurrence
 * @property {(documentId: number) => Array<{ file_path: string }>} getOccurrences
 * @property {(documentId: number) => number} startAttempt
 * @property {(documentId: number, fields: object) => void} updateDocument
 * @property {() => number} resetInterrupted
 * @property {(statuses: string[]) => object[]} listDocumentsByStatus
 * @property {(run: object) => number} insertExtractionRun
 * @property {(result: object) => number} saveInvoiceResult
 * @property {(businessKey: string, excludeDocumentId: number) => object[]} findByBusinessKey
 */
export {};
