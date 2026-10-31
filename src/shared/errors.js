/**
 * Códigos de salida públicos de la CLI. Están documentados en el README:
 * cambiarlos rompe a quienes automatizan InvoiceBot desde otros scripts.
 */
export const EXIT_CODES = Object.freeze({
  OK: 0,
  GENERAL_ERROR: 1,
  USAGE_ERROR: 2,
  PARTIAL: 3,
});

export class InvoiceBotError extends Error {
  constructor(
    message,
    { code = 'INVOICEBOT_ERROR', exitCode = EXIT_CODES.GENERAL_ERROR, cause } = {},
  ) {
    super(message, { cause });
    this.name = this.constructor.name;
    this.code = code;
    this.exitCode = exitCode;
  }
}

/** Configuración o argumentos incorrectos (código de salida 2). */
export class ConfigError extends InvoiceBotError {
  constructor(message, options = {}) {
    super(message, { code: 'CONFIG_ERROR', exitCode: EXIT_CODES.USAGE_ERROR, ...options });
  }
}

/**
 * Fallo de extracción de un documento concreto. `retryable` indica si repetir
 * la misma operación tiene sentido (red, límite de tasa, JSON inválido) o no
 * (documento cifrado, rechazo del modelo).
 */
export class ExtractionError extends InvoiceBotError {
  constructor(message, { kind = 'unknown', retryable = false, rawResponse = null, cause } = {}) {
    super(message, { code: 'EXTRACTION_ERROR', cause });
    this.kind = kind;
    this.retryable = retryable;
    this.rawResponse = rawResponse;
  }
}

/** Acción de revisión no permitida (factura inexistente, sin nota, con errores…). Código de salida 2. */
export class ReviewError extends InvoiceBotError {
  constructor(message, options = {}) {
    super(message, { code: 'REVIEW_ERROR', exitCode: EXIT_CODES.USAGE_ERROR, ...options });
  }
}

export class DocumentReadError extends InvoiceBotError {
  constructor(message, { cause } = {}) {
    super(message, { code: 'DOCUMENT_READ_ERROR', cause });
  }
}
