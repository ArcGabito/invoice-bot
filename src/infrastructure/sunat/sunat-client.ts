import { ConfigError, InvoiceBotError, errorMessage } from '../../shared/errors.ts';
import type { Config } from '../../shared/config.ts';
import { formatCents } from '../../shared/money.ts';

/**
 * Cliente del servicio "Consulta Integrada de Comprobante de Pago" de SUNAT
 * (Manual de Consulta Integrada de Comprobante de Pago por Servicio Web v2.0).
 *
 * Una sola consulta devuelve el estado del comprobante, el estado del
 * contribuyente emisor y su condición de domicilio. Requiere credenciales
 * de API SUNAT (client_id y client_secret) generadas en SUNAT Operaciones en
 * Línea con la Clave SOL de la empresa que consulta.
 */
export const SUNAT_AUTH_URL = 'https://api-seguridad.sunat.gob.pe/v1/clientesextranet';
export const SUNAT_API_URL = 'https://api.sunat.gob.pe/v1/contribuyente/contribuyentes';
const SCOPE = 'https://api.sunat.gob.pe/v1/contribuyente/contribuyentes';

/** Tipos de comprobante (codComp) que admite el servicio. */
export const SUPPORTED_DOCUMENT_TYPES = new Set(['01', '03', '04', '07', '08', 'R1', 'R7']);

/** Margen para renovar el token antes de que caduque. */
const TOKEN_SAFETY_MARGIN_MS = 60_000;

export type SunatErrorKind = 'unknown' | 'transient' | 'sunat_auth' | 'sunat_rejected';

export class SunatError extends InvoiceBotError {
  kind: SunatErrorKind;
  retryable: boolean;

  constructor(
    message: string,
    {
      kind = 'unknown',
      retryable = false,
      cause,
    }: { kind?: SunatErrorKind; retryable?: boolean; cause?: unknown } = {},
  ) {
    super(message, { code: 'SUNAT_ERROR', cause });
    this.kind = kind;
    this.retryable = retryable;
  }
}

/** Resultado de una consulta: los códigos que interpreta el dominio y la respuesta completa. */
export interface SunatReceiptResult {
  estadoCp: string | null;
  estadoRuc: string | null;
  condDomiRuc: string | null;
  observaciones: string[];
  raw: unknown;
}

export interface ReceiptQuery {
  supplierRuc: string;
  documentType: string;
  series: string;
  number: string;
  issueDate: string;
  totalCents: number | null | undefined;
}

interface SunatPayload {
  success?: boolean;
  errorCode?: string;
  message?: string;
  data?: {
    estadoCp?: string | number | null;
    estadoRuc?: string | null;
    condDomiRuc?: string | null;
    observaciones?: unknown;
    Observaciones?: unknown;
  };
}

export class SunatClient {
  readonly ruc: string;
  readonly clientId: string;
  readonly clientSecret: string;
  readonly fetch: typeof globalThis.fetch;
  readonly authUrl: string;
  readonly apiUrl: string;
  readonly timeoutMs: number;
  readonly now: () => number;
  private token: { value: string; expiresAt: number } | null;

  constructor({
    ruc,
    clientId,
    clientSecret,
    fetchImpl = globalThis.fetch,
    authUrl = SUNAT_AUTH_URL,
    apiUrl = SUNAT_API_URL,
    timeoutMs = 20_000,
    now = () => Date.now(),
  }: {
    ruc: string;
    clientId: string;
    clientSecret: string;
    fetchImpl?: typeof globalThis.fetch;
    authUrl?: string;
    apiUrl?: string;
    timeoutMs?: number;
    now?: () => number;
  }) {
    this.ruc = ruc;
    this.clientId = clientId;
    this.clientSecret = clientSecret;
    this.fetch = fetchImpl;
    this.authUrl = authUrl.replace(/\/$/, '');
    this.apiUrl = apiUrl.replace(/\/$/, '');
    this.timeoutMs = timeoutMs;
    this.now = now;
    this.token = null;
  }

  async getToken({ refresh = false }: { refresh?: boolean } = {}): Promise<string> {
    if (!refresh && this.token && this.token.expiresAt > this.now()) return this.token.value;
    const res = await this.request(
      `${this.authUrl}/${encodeURIComponent(this.clientId)}/oauth2/token/`,
      {
        method: 'POST',
        headers: { 'content-type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams({
          grant_type: 'client_credentials',
          scope: SCOPE,
          client_id: this.clientId,
          client_secret: this.clientSecret,
        }),
      },
    );
    if (res.status === 400 || res.status === 401) {
      throw new SunatError('SUNAT rechazó las credenciales de API (client_id o client_secret)', {
        kind: 'sunat_auth',
      });
    }
    const body = (await this.json(res)) as {
      access_token?: string;
      expires_in?: number | string;
    } | null;
    if (!body?.access_token) {
      throw new SunatError(`SUNAT no devolvió un token (HTTP ${res.status})`, {
        kind: res.status >= 500 ? 'transient' : 'sunat_auth',
        retryable: res.status >= 500,
      });
    }
    const ttlMs = Number(body.expires_in ?? 3600) * 1000;
    this.token = {
      value: body.access_token,
      expiresAt: this.now() + ttlMs - TOKEN_SAFETY_MARGIN_MS,
    };
    return this.token.value;
  }

  /**
   * Consulta un comprobante. `totalCents` solo se envía para comprobantes
   * electrónicos (serie que empieza con letra), como indica el manual.
   */
  async validateReceipt({
    supplierRuc,
    documentType,
    series,
    number,
    issueDate,
    totalCents,
  }: ReceiptQuery): Promise<SunatReceiptResult> {
    const [year, month, day] = String(issueDate).split('-');
    const body: Record<string, string | number> = {
      numRuc: supplierRuc,
      codComp: documentType,
      numeroSerie: series,
      numero: Number(number),
      fechaEmision: `${day}/${month}/${year}`,
    };
    if (/^[A-Z]/i.test(series) && totalCents !== null && totalCents !== undefined) {
      body.monto = Number(formatCents(totalCents));
    }

    const url = `${this.apiUrl}/${this.ruc}/validarcomprobante`;
    const send = async (token: string) =>
      this.request(url, {
        method: 'POST',
        headers: { 'content-type': 'application/json', authorization: `Bearer ${token}` },
        body: JSON.stringify(body),
      });

    let res = await send(await this.getToken());
    if (res.status === 401) res = await send(await this.getToken({ refresh: true }));
    if (res.status === 429 || res.status >= 500) {
      throw new SunatError(`Servicio de SUNAT no disponible (HTTP ${res.status})`, {
        kind: 'transient',
        retryable: true,
      });
    }
    const payload = (await this.json(res)) as SunatPayload | null;
    if (!res.ok || payload?.success === false) {
      const detail = [payload?.errorCode, payload?.message].filter(Boolean).join(': ');
      throw new SunatError(
        `SUNAT rechazó la consulta${detail ? ` (${detail})` : ` (HTTP ${res.status})`}`,
        {
          kind: res.status === 401 || res.status === 403 ? 'sunat_auth' : 'sunat_rejected',
        },
      );
    }
    const data = payload?.data ?? {};
    const observaciones = data.observaciones ?? data.Observaciones ?? [];
    return {
      estadoCp:
        data.estadoCp === undefined || data.estadoCp === null ? null : String(data.estadoCp),
      estadoRuc: data.estadoRuc ?? null,
      condDomiRuc: data.condDomiRuc ?? null,
      observaciones: Array.isArray(observaciones) ? observaciones.map(String) : [],
      raw: payload,
    };
  }

  /** Comprueba las credenciales obteniendo un token nuevo. */
  async ping(): Promise<string> {
    await this.getToken({ refresh: true });
    return `credenciales válidas para el RUC ${this.ruc}`;
  }

  async request(url: string, init: RequestInit): Promise<Response> {
    try {
      return await this.fetch(url, { ...init, signal: AbortSignal.timeout(this.timeoutMs) });
    } catch (error) {
      throw new SunatError(`No se pudo conectar con SUNAT: ${errorMessage(error)}`, {
        kind: 'transient',
        retryable: true,
        cause: error,
      });
    }
  }

  async json(res: Response): Promise<unknown> {
    try {
      return await res.json();
    } catch {
      return null;
    }
  }
}

/**
 * Crea el cliente a partir de la configuración; el secreto se lee del entorno.
 * La verificación en SUNAT es opcional y debe activarse explícitamente.
 */
export function createSunatClient(
  config: Config,
  env: Record<string, string | undefined> = process.env,
): SunatClient {
  const { enabled, ruc, clientId, clientSecretSet, timeoutMs, authUrl, apiUrl } = config.sunat;
  if (!enabled) {
    throw new ConfigError(
      'La verificación en SUNAT está desactivada. Para usarla, define INVOICEBOT_SUNAT_ENABLED=true ' +
        'y las credenciales de API SUNAT en .env.',
    );
  }
  const missing = [
    !ruc && 'INVOICEBOT_SUNAT_RUC',
    !clientId && 'INVOICEBOT_SUNAT_CLIENT_ID',
    !clientSecretSet && 'INVOICEBOT_SUNAT_CLIENT_SECRET',
  ].filter(Boolean);
  if (missing.length || !ruc || !clientId) {
    throw new ConfigError(
      `Faltan credenciales de API SUNAT: ${missing.join(', ')}. ` +
        'Se generan en SUNAT Operaciones en Línea → Credenciales de API SUNAT.',
    );
  }
  return new SunatClient({
    ruc,
    clientId,
    clientSecret: env.INVOICEBOT_SUNAT_CLIENT_SECRET ?? '',
    timeoutMs,
    ...(authUrl && { authUrl }),
    ...(apiUrl && { apiUrl }),
  });
}
