/**
 * Puerto InvoiceRepository: persistencia de documentos, extracciones,
 * facturas e incidencias. Su forma es la de la implementación con SQL Server
 * (src/infrastructure/database/repositories/invoice-repository.ts); los casos
 * de uso dependen solo de este tipo. Garantías que debe respetar cualquier
 * otra implementación:
 * - `saveInvoiceResult` es atómico (factura + incidencias + estado del documento).
 * - `insertDocument` rechaza un SHA-256 ya registrado.
 * - `transaction` ejecuta la función con un repositorio ligado a la transacción.
 * - `lockBusinessKey` serializa, hasta el final de la transacción, a quienes
 *   buscan duplicados de una misma clave de negocio y guardan la factura.
 */
export type { InvoiceRepository } from '../../infrastructure/database/repositories/invoice-repository.ts';
export type { UserRepository } from '../../infrastructure/database/repositories/user-repository.ts';
