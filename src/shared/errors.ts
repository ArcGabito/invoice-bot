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

export type ExitCode = (typeof EXIT_CODES)[keyof typeof EXIT_CODES];

export interface InvoiceBotErrorOptions {
  code?: string;
  exitCode?: ExitCode;
  cause?: unknown;
}

export class InvoiceBotError extends Error {
  code: string;
  exitCode: ExitCode;

  constructor(
    message: string,
    {
      code = 'INVOICEBOT_ERROR',
      exitCode = EXIT_CODES.GENERAL_ERROR,
      cause,
    }: InvoiceBotErrorOptions = {},
  ) {
    super(message, { cause });
    this.name = this.constructor.name;
    this.code = code;
    this.exitCode = exitCode;
  }
}

/** Configuración o argumentos incorrectos (código de salida 2). */
export class ConfigError extends InvoiceBotError {
  constructor(message: string, options: InvoiceBotErrorOptions = {}) {
    super(message, { code: 'CONFIG_ERROR', exitCode: EXIT_CODES.USAGE_ERROR, ...options });
  }
}

export type ExtractionErrorKind =
  | 'unknown'
  | 'invalid_output'
  | 'refused'
  | 'transient'
  | 'timeout'
  | 'provider_auth'
  | 'provider_config'
  | 'provider_request'
  | 'file_too_large'
  | 'not_invoice'
  | (string & {});

/**
 * Fallo de extracción de un documento concreto. `retryable` indica si repetir
 * la misma operación tiene sentido (red, límite de tasa, JSON inválido) o no
 * (documento cifrado, rechazo del modelo).
 */
export class ExtractionError extends InvoiceBotError {
  kind: ExtractionErrorKind;
  retryable: boolean;
  rawResponse: string | null;

  constructor(
    message: string,
    {
      kind = 'unknown',
      retryable = false,
      rawResponse = null,
      cause,
    }: {
      kind?: ExtractionErrorKind;
      retryable?: boolean;
      rawResponse?: string | null;
      cause?: unknown;
    } = {},
  ) {
    super(message, { code: 'EXTRACTION_ERROR', cause });
    this.kind = kind;
    this.retryable = retryable;
    this.rawResponse = rawResponse;
  }
}

/** Acción de revisión no permitida (factura inexistente, sin nota, con errores…). Código de salida 2. */
export class ReviewError extends InvoiceBotError {
  constructor(message: string, options: InvoiceBotErrorOptions = {}) {
    super(message, { code: 'REVIEW_ERROR', exitCode: EXIT_CODES.USAGE_ERROR, ...options });
  }
}

/** Alta o cambio de usuario no válido (nombre repetido, contraseña corta…). Código de salida 2. */
export class UserError extends InvoiceBotError {
  constructor(message: string, options: InvoiceBotErrorOptions = {}) {
    super(message, { code: 'USER_ERROR', exitCode: EXIT_CODES.USAGE_ERROR, ...options });
  }
}

export class DocumentReadError extends InvoiceBotError {
  constructor(message: string, { cause }: { cause?: unknown } = {}) {
    super(message, { code: 'DOCUMENT_READ_ERROR', cause });
  }
}

/** Mensaje de un valor lanzado, sea o no un Error. */
export const errorMessage = (error: unknown): string =>
  error instanceof Error ? error.message : String(error);
