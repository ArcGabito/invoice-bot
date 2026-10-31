import { describe, expect, it, vi } from 'vitest';
import { evaluateSunatResult } from '../../src/domain/sunat-status.ts';
import { SunatClient, createSunatClient } from '../../src/infrastructure/sunat/sunat-client.ts';
import { loadConfig } from '../../src/shared/config.ts';

const ok = (estadoCp, extra = {}) => ({
  estadoCp,
  estadoRuc: '00',
  condDomiRuc: '00',
  observaciones: [],
  ...extra,
});
const codes = (r) => r.issues.map((i) => [i.rule_code, i.severity]);

describe('evaluateSunatResult', () => {
  it('no genera incidencias para un comprobante aceptado de un emisor activo y habido', async () => {
    expect(evaluateSunatResult(ok('1'), { documentType: '01' })).toEqual({
      receiptStatus: 'ACEPTADO',
      issues: [],
    });
    expect(evaluateSunatResult(ok('3'), { documentType: '01' }).receiptStatus).toBe('AUTORIZADO');
  });

  it('marca como error una factura anulada, no informada o no autorizada', async () => {
    expect(codes(evaluateSunatResult(ok('2'), { documentType: '01' }))).toEqual([
      ['SUNAT_RECEIPT_VOIDED', 'error'],
    ]);
    expect(codes(evaluateSunatResult(ok('0'), { documentType: '01' }))).toEqual([
      ['SUNAT_RECEIPT_NOT_FOUND', 'error'],
    ]);
    expect(codes(evaluateSunatResult(ok('4'), { documentType: '01' }))).toEqual([
      ['SUNAT_RECEIPT_NOT_AUTHORIZED', 'error'],
    ]);
  });

  it('solo advierte cuando una boleta aún no aparece (resumen diario)', () => {
    expect(codes(evaluateSunatResult(ok('0'), { documentType: '03' }))).toEqual([
      ['SUNAT_RECEIPT_NOT_FOUND', 'warning'],
    ]);
  });

  it('advierte sobre emisores no activos y no habidos, con el estado legible', async () => {
    const result = evaluateSunatResult(ok('1', { estadoRuc: '10', condDomiRuc: '12' }), {
      documentType: '01',
    });
    expect(result.issues).toEqual([
      expect.objectContaining({
        rule_code: 'SUNAT_SUPPLIER_NOT_ACTIVE',
        severity: 'warning',
        actual_value: 'BAJA DEFINITIVA',
      }),
      expect.objectContaining({
        rule_code: 'SUNAT_SUPPLIER_NOT_HABIDO',
        severity: 'warning',
        actual_value: 'NO HABIDO',
      }),
    ]);
  });

  it('guarda las observaciones de SUNAT como información', async () => {
    const result = evaluateSunatResult(ok('1', { observaciones: ['- El monto no coincide'] }), {
      documentType: '01',
    });
    expect(codes(result)).toEqual([['SUNAT_OBSERVATION', 'info']]);
  });
});

/** fetch simulado que registra las peticiones y responde en orden. */
function fakeFetch(...responses) {
  const calls: { url: string; init: any }[] = [];
  const fn = vi.fn(async (url: string, init: any) => {
    calls.push({ url, init });
    const next = responses.shift();
    if (next instanceof Error) throw next;
    return {
      ok: next.status >= 200 && next.status < 300,
      status: next.status,
      json: async () => next.body,
    };
  });
  return Object.assign(fn, { calls });
}

const TOKEN = { status: 200, body: { access_token: 'tok-1', token_type: 'JWT', expires_in: 3600 } };
const VALID = {
  status: 200,
  body: {
    success: true,
    message: 'Operation Success! ',
    data: { estadoCp: '1', estadoRuc: '00', condDomiRuc: '00' },
  },
};

function client(fetchImpl, extra = {}) {
  return new SunatClient({
    ruc: '20555555551',
    clientId: 'client id',
    clientSecret: 's3cr3t',
    fetchImpl,
    ...extra,
  });
}

const receipt = {
  supplierRuc: '20100047218',
  documentType: '01',
  series: 'F001',
  number: '00001234',
  issueDate: '2026-10-05',
  totalCents: 11800,
};

describe('SunatClient', () => {
  it('pide el token y consulta con el formato del manual de SUNAT', async () => {
    const fetchImpl = fakeFetch(TOKEN, VALID);
    const result = await client(fetchImpl).validateReceipt(receipt);

    const [tokenCall, queryCall] = fetchImpl.calls;
    expect(tokenCall.url).toBe(
      'https://api-seguridad.sunat.gob.pe/v1/clientesextranet/client%20id/oauth2/token/',
    );
    expect(tokenCall.init.headers['content-type']).toBe('application/x-www-form-urlencoded');
    expect(Object.fromEntries(tokenCall.init.body)).toEqual({
      grant_type: 'client_credentials',
      scope: 'https://api.sunat.gob.pe/v1/contribuyente/contribuyentes',
      client_id: 'client id',
      client_secret: 's3cr3t',
    });

    expect(queryCall.url).toBe(
      'https://api.sunat.gob.pe/v1/contribuyente/contribuyentes/20555555551/validarcomprobante',
    );
    expect(queryCall.init.headers.authorization).toBe('Bearer tok-1');
    expect(JSON.parse(queryCall.init.body)).toEqual({
      numRuc: '20100047218',
      codComp: '01',
      numeroSerie: 'F001',
      numero: 1234,
      fechaEmision: '05/10/2026',
      monto: 118,
    });
    expect(result).toMatchObject({
      estadoCp: '1',
      estadoRuc: '00',
      condDomiRuc: '00',
      observaciones: [],
    });
  });

  it('no envía el monto para comprobantes físicos (serie numérica)', async () => {
    const fetchImpl = fakeFetch(TOKEN, VALID);
    await client(fetchImpl).validateReceipt({ ...receipt, series: '0001' });
    expect(JSON.parse(fetchImpl.calls[1].init.body)).not.toHaveProperty('monto');
  });

  it('reutiliza el token hasta poco antes de que caduque', async () => {
    let now = 0;
    const fetchImpl = fakeFetch(TOKEN, VALID, VALID, TOKEN, VALID);
    const c = client(fetchImpl, { now: () => now });
    await c.validateReceipt(receipt);
    now = 30 * 60 * 1000;
    await c.validateReceipt(receipt);
    expect(fetchImpl).toHaveBeenCalledTimes(3);
    now = 59.5 * 60 * 1000; // dentro del margen de seguridad de 60 s
    await c.validateReceipt(receipt);
    expect(fetchImpl).toHaveBeenCalledTimes(5);
  });

  it('renueva el token una vez si SUNAT responde 401', async () => {
    const fetchImpl = fakeFetch(TOKEN, { status: 401, body: {} }, TOKEN, VALID);
    const result = await client(fetchImpl).validateReceipt(receipt);
    expect(result.estadoCp).toBe('1');
    expect(fetchImpl).toHaveBeenCalledTimes(4);
  });

  it('acepta las observaciones con mayúscula, como las escribe el manual', async () => {
    const body = { success: true, data: { estadoCp: 2, Observaciones: ['- comunicado de baja'] } };
    const result = await client(fakeFetch(TOKEN, { status: 200, body })).validateReceipt(receipt);
    expect(result).toMatchObject({ estadoCp: '2', observaciones: ['- comunicado de baja'] });
  });

  it('clasifica los errores para saber si conviene reintentar', async () => {
    await expect(
      client(fakeFetch({ status: 401, body: { error: 'invalid_client' } })).validateReceipt(
        receipt,
      ),
    ).rejects.toMatchObject({ kind: 'sunat_auth', retryable: false });
    await expect(
      client(fakeFetch(TOKEN, { status: 503, body: null })).validateReceipt(receipt),
    ).rejects.toMatchObject({ kind: 'transient', retryable: true });
    await expect(
      client(fakeFetch(new TypeError('fetch failed'))).validateReceipt(receipt),
    ).rejects.toMatchObject({ kind: 'transient', retryable: true });
    await expect(
      client(
        fakeFetch(TOKEN, {
          status: 200,
          body: { success: false, message: 'El numero de RUC es invalido', errorCode: '0001' },
        }),
      ).validateReceipt(receipt),
    ).rejects.toMatchObject({ kind: 'sunat_rejected', message: expect.stringMatching(/0001/) });
  });
});

describe('createSunatClient', () => {
  const config = (env) => loadConfig({ env, loadEnvFile: false });

  const credentials = {
    INVOICEBOT_SUNAT_RUC: '20555555551',
    INVOICEBOT_SUNAT_CLIENT_ID: 'id',
    INVOICEBOT_SUNAT_CLIENT_SECRET: 'secreto',
  };

  it('está desactivada por defecto aunque haya credenciales', async () => {
    const cfg = config(credentials);
    expect(cfg.sunat).toMatchObject({ enabled: false, verifyOnScan: false });
    expect(() => createSunatClient(cfg, credentials)).toThrow(/INVOICEBOT_SUNAT_ENABLED=true/);
  });

  it('activada, exige RUC, client_id y client_secret, sin copiar el secreto a la configuración', async () => {
    expect(() => createSunatClient(config({ INVOICEBOT_SUNAT_ENABLED: 'true' }), {})).toThrow(
      /INVOICEBOT_SUNAT_CLIENT_ID/,
    );
    const env = { ...credentials, INVOICEBOT_SUNAT_ENABLED: 'true' };
    const cfg = config(env);
    expect(JSON.stringify(cfg)).not.toContain('secreto');
    expect(createSunatClient(cfg, env)).toBeInstanceOf(SunatClient);
  });

  it('aplica tiempo de espera y URLs configurables', async () => {
    const env = {
      ...credentials,
      INVOICEBOT_SUNAT_ENABLED: 'sí',
      INVOICEBOT_SUNAT_TIMEOUT_MS: '5000',
      INVOICEBOT_SUNAT_AUTH_URL: 'http://proxy.local/auth/',
      INVOICEBOT_SUNAT_API_URL: 'http://proxy.local/api',
    };
    const client = createSunatClient(config(env), env);
    expect(client).toMatchObject({
      timeoutMs: 5000,
      authUrl: 'http://proxy.local/auth',
      apiUrl: 'http://proxy.local/api',
    });
    // Sin URLs propias se usan las oficiales.
    const official = createSunatClient(
      config({ ...credentials, INVOICEBOT_SUNAT_ENABLED: '1' }),
      credentials,
    );
    expect(official.apiUrl).toBe('https://api.sunat.gob.pe/v1/contribuyente/contribuyentes');
  });

  it('valida los valores de configuración', async () => {
    expect(() => config({ INVOICEBOT_SUNAT_RUC: '123' })).toThrow(/sunat\.ruc/);
    expect(() => config({ INVOICEBOT_SUNAT_ENABLED: 'quizás' })).toThrow(/sunat\.enabled/);
    expect(() => config({ INVOICEBOT_SUNAT_VERIFY_ON_SCAN: 'true' })).toThrow(
      /requiere INVOICEBOT_SUNAT_ENABLED=true/,
    );
    expect(() => config({ INVOICEBOT_SUNAT_TIMEOUT_MS: '10' })).toThrow(/sunat\.timeoutMs/);
    expect(() => config({ INVOICEBOT_SUNAT_API_URL: 'no es una url' })).toThrow(/sunat\.apiUrl/);
  });
});
