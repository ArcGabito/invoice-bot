import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { describe, expect, it, vi } from 'vitest';
import { messageFolder } from '../../src/application/use-cases/fetch-mail.ts';
import { extractMailFiles, safeFileName } from '../../src/infrastructure/mail/attachments.ts';
import { hasAttachmentPart, defaultImapHost } from '../../src/infrastructure/mail/imap-source.ts';
import { createMailSource } from '../../src/infrastructure/mail/mail-factory.ts';
import { MailError } from '../../src/infrastructure/mail/mail-source.ts';
import {
  clientCredentialsProvider,
  googleServiceAccountProvider,
  refreshTokenProvider,
} from '../../src/infrastructure/mail/oauth.ts';
import { readMailToken, writeMailToken } from '../../src/infrastructure/mail/token-store.ts';
import { loadConfig } from '../../src/shared/config.ts';
import { ConfigError } from '../../src/shared/errors.ts';
import { buildMimeMessage, buildZip } from '../helpers/mail-builder.ts';
import { buildUblInvoice } from '../helpers/ubl-builder.ts';

const PDF = Buffer.from('%PDF-1.7\n% factura de prueba\n');
const BIG_IMAGE = Buffer.alloc(30 * 1024, 7);

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

const config = (env: Record<string, string>) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'invoicebot-mail-'));
  return { dir, config: loadConfig({ env, cwd: dir, loadEnvFile: false }) };
};

describe('adjuntos del correo', () => {
  it('guarda XML, PDF e imágenes adjuntas; omite logos incrustados y otros tipos', async () => {
    const mail = await extractMailFiles(
      buildMimeMessage({
        attachments: [
          {
            name: '20100047218-01-F001-00001234.xml',
            type: 'application/xml',
            content: buildUblInvoice(),
          },
          { name: '20100047218-01-F001-00001234.pdf', type: 'application/pdf', content: PDF },
          { name: 'foto-boleta.jpg', type: 'image/jpeg', content: BIG_IMAGE },
          { name: 'logo.png', type: 'image/png', content: BIG_IMAGE, inline: true },
          { name: 'icono.png', type: 'image/png', content: Buffer.alloc(500) },
          { name: 'contrato.docx', type: 'application/octet-stream', content: 'x' },
        ],
      }),
    );
    expect(mail).toMatchObject({
      sender: 'facturacion@proveedor.pe',
      subject: 'Factura electrónica F001-00001234',
      date: '2026-10-15T15:00:00.000Z',
    });
    expect(mail.files.map((f) => f.name)).toEqual([
      '20100047218-01-F001-00001234.xml',
      '20100047218-01-F001-00001234.pdf',
      'foto-boleta.jpg',
    ]);
    expect(mail.files[1].content.equals(PDF)).toBe(true);
    expect(mail.skipped).toEqual([
      'icono.png: imagen demasiado pequeña para ser un comprobante',
      'contrato.docx: tipo de archivo no soportado',
    ]);
  });

  it('abre los ZIP y toma solo los comprobantes', async () => {
    const zip = buildZip({
      'FACTURA/R-20100047218-01-F001-1.xml': '<ApplicationResponse/>',
      'FACTURA/20100047218-01-F001-1.xml': buildUblInvoice(),
      'FACTURA/20100047218-01-F001-1.pdf': PDF,
      '__MACOSX/FACTURA/._20100047218-01-F001-1.pdf': 'x',
      'leeme.txt': 'hola',
    });
    const mail = await extractMailFiles(
      buildMimeMessage({
        attachments: [{ name: 'F001-1.zip', type: 'application/zip', content: zip }],
      }),
    );
    expect(mail.files.map((f) => f.name).sort()).toEqual([
      '20100047218-01-F001-1.pdf',
      '20100047218-01-F001-1.xml',
      'R-20100047218-01-F001-1.xml',
    ]);
    const broken = await extractMailFiles(
      buildMimeMessage({
        attachments: [{ name: 'roto.zip', type: 'application/zip', content: 'no es zip' }],
      }),
    );
    expect(broken.files).toEqual([]);
    expect(broken.skipped[0]).toMatch(/^roto\.zip: ZIP dañado/);
  });

  it('no permite que un nombre de adjunto salga de su carpeta y evita nombres repetidos', async () => {
    expect(safeFileName('../../Windows/system32/x.pdf')).toBe('x.pdf');
    expect(safeFileName('..\\..\\x.xml')).toBe('x.xml');
    expect(safeFileName('fac:tura?.pdf')).toBe('fac_tura_.pdf');
    expect(safeFileName('CON.xml')).toBe('adjunto.xml');
    expect(safeFileName('a'.repeat(300) + '.pdf')).toHaveLength(124);
    const mail = await extractMailFiles(
      buildMimeMessage({
        attachments: [
          { name: 'factura.pdf', type: 'application/pdf', content: PDF },
          { name: 'FACTURA.pdf', type: 'application/pdf', content: PDF },
        ],
      }),
    );
    expect(mail.files.map((f) => f.name)).toEqual(['factura.pdf', 'FACTURA-2.pdf']);
  });

  it('agrupa por mes y deja una carpeta por mensaje', () => {
    expect(messageFolder(new Date('2026-10-15T15:00:09Z'), 'abc')).toMatch(
      /^2026-10[\\/]20261015-150009-[0-9a-f]{8}$/,
    );
  });

  it('IMAP detecta adjuntos por la estructura del mensaje', () => {
    const text = { type: 'text/plain', parameters: { charset: 'utf-8' } };
    expect(
      hasAttachmentPart({
        type: 'multipart/alternative',
        childNodes: [text, { type: 'text/html' }],
      }),
    ).toBe(false);
    expect(
      hasAttachmentPart({
        type: 'multipart/mixed',
        childNodes: [text, { type: 'application/pdf', disposition: 'attachment' }],
      }),
    ).toBe(true);
    expect(hasAttachmentPart({ type: 'application/xml', parameters: { name: 'f.xml' } })).toBe(
      true,
    );
    expect(defaultImapHost('pcjhonny13@gmail.com')).toBe('imap.gmail.com');
    expect(defaultImapHost('facturas@empresa.pe')).toBeNull();
  });
});

describe('tokens OAuth', () => {
  it('renueva con el refresh token, lo reutiliza mientras vale y guarda el nuevo', async () => {
    const fetchImpl = vi.fn(async (_url: string | URL | Request, init?: RequestInit) => {
      const form = new URLSearchParams(String(init?.body));
      expect(form.get('grant_type')).toBe('refresh_token');
      return json({
        access_token: `at-${form.get('refresh_token')}`,
        refresh_token: 'rt-2',
        expires_in: 3600,
      });
    });
    const onRotate = vi.fn();
    const tokens = refreshTokenProvider({
      tokenUrl: 'http://login.test/token',
      clientId: 'cid',
      refreshToken: 'rt-1',
      onRotate,
      fetchImpl: fetchImpl as typeof fetch,
    });
    expect(await Promise.all([tokens.getToken(), tokens.getToken()])).toEqual([
      'at-rt-1',
      'at-rt-1',
    ]);
    expect(await tokens.getToken()).toBe('at-rt-1');
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    expect(onRotate).toHaveBeenCalledWith('rt-2');
  });

  it('explica un acceso vencido o unas credenciales rechazadas', async () => {
    const expired = refreshTokenProvider({
      tokenUrl: 'http://login.test/token',
      clientId: 'cid',
      refreshToken: 'viejo',
      fetchImpl: async () =>
        json(
          { error: 'invalid_grant', error_description: 'Token has been expired or revoked.' },
          400,
        ),
    });
    const error = await expired.getToken().catch((e) => e);
    expect(error).toBeInstanceOf(MailError);
    expect(error.auth).toBe(true);
    expect(error.message).toMatch(/expired or revoked.*invoicebot mail login/);

    const app = clientCredentialsProvider({
      tokenUrl: 'http://login.test/token',
      clientId: 'cid',
      clientSecret: 'mal',
      scope: 'https://graph.microsoft.com/.default',
      fetchImpl: async () =>
        json(
          {
            error: 'invalid_client',
            error_description: 'AADSTS7000215: Invalid client secret provided.',
          },
          401,
        ),
    });
    await expect(app.getToken()).rejects.toThrow(/Invalid client secret.*client secret/);
  });

  it('firma el JWT de la cuenta de servicio de Google a nombre del buzón', async () => {
    const { privateKey, publicKey } = crypto.generateKeyPairSync('rsa', { modulusLength: 2048 });
    const tokens = googleServiceAccountProvider({
      key: {
        client_email: 'invoicebot@proyecto.iam.gserviceaccount.com',
        private_key: privateKey.export({ type: 'pkcs8', format: 'pem' }).toString(),
        token_uri: 'http://google.test/token',
      },
      subject: 'facturas@empresa.pe',
      scope: 'https://www.googleapis.com/auth/gmail.readonly',
      fetchImpl: async (_url, init) => {
        const assertion = new URLSearchParams(String(init?.body)).get('assertion')!;
        const [header, payload, signature] = assertion.split('.');
        const valid = crypto.verify(
          'RSA-SHA256',
          Buffer.from(`${header}.${payload}`),
          publicKey,
          Buffer.from(signature, 'base64url'),
        );
        const claims = JSON.parse(Buffer.from(payload, 'base64url').toString());
        expect(claims).toMatchObject({
          iss: 'invoicebot@proyecto.iam.gserviceaccount.com',
          sub: 'facturas@empresa.pe',
          aud: 'http://google.test/token',
        });
        return valid ? json({ access_token: 'sa-token', expires_in: 3600 }) : json({}, 400);
      },
    });
    expect(await tokens.getToken()).toBe('sa-token');
  });
});

describe('configuración del correo', () => {
  it('está desactivada por defecto y pide lo que falta de cada proveedor', () => {
    const missing = (env: Record<string, string>) => {
      try {
        createMailSource(config(env).config, env);
        return null;
      } catch (error) {
        expect(error).toBeInstanceOf(ConfigError);
        return (error as Error).message;
      }
    };
    expect(missing({})).toMatch(/desactivada.*INVOICEBOT_MAIL_PROVIDER/);
    expect(
      missing({ INVOICEBOT_MAIL_PROVIDER: 'imap', INVOICEBOT_MAIL_USER: 'facturas@empresa.pe' }),
    ).toMatch(/INVOICEBOT_MAIL_PASSWORD, INVOICEBOT_MAIL_IMAP_HOST/);
    // Gmail por IMAP: el servidor se deduce de la dirección.
    expect(
      missing({
        INVOICEBOT_MAIL_PROVIDER: 'imap',
        INVOICEBOT_MAIL_USER: 'ana@gmail.com',
        INVOICEBOT_MAIL_PASSWORD: 'abcd efgh ijkl mnop',
      }),
    ).toBeNull();
    expect(missing({ INVOICEBOT_MAIL_PROVIDER: 'gmail' })).toMatch(
      /GOOGLE_CLIENT_ID, INVOICEBOT_MAIL_GOOGLE_CLIENT_SECRET/,
    );
    expect(
      missing({
        INVOICEBOT_MAIL_PROVIDER: 'gmail',
        INVOICEBOT_MAIL_GOOGLE_CLIENT_ID: 'id',
        INVOICEBOT_MAIL_GOOGLE_CLIENT_SECRET: 's',
      }),
    ).toMatch(/invoicebot mail login/);
    expect(missing({ INVOICEBOT_MAIL_PROVIDER: 'graph' })).toMatch(/INVOICEBOT_MAIL_MS_CLIENT_ID/);
    expect(
      missing({
        INVOICEBOT_MAIL_PROVIDER: 'graph',
        INVOICEBOT_MAIL_MS_CLIENT_ID: 'id',
        INVOICEBOT_MAIL_MS_CLIENT_SECRET: 's',
        INVOICEBOT_MAIL_USER: 'facturas@empresa.pe',
      }),
    ).toMatch(/INVOICEBOT_MAIL_MS_TENANT/);
    expect(() => config({ INVOICEBOT_MAIL_PROVIDER: 'pop3' })).toThrow(/mail\.provider/);
  });

  it('usa el acceso guardado por mail login del mismo proveedor y aplicación', () => {
    const env = {
      INVOICEBOT_MAIL_PROVIDER: 'graph',
      INVOICEBOT_MAIL_MS_CLIENT_ID: 'app-1',
    };
    const { config: cfg } = config(env);
    writeMailToken(cfg.mail.tokenFile, {
      provider: 'graph',
      clientId: 'app-1',
      account: 'a@outlook.com',
      refreshToken: 'rt',
    });
    expect(createMailSource(cfg, env).provider).toBe('graph');
    expect(readMailToken(cfg.mail.tokenFile)).toMatchObject({
      account: 'a@outlook.com',
      refreshToken: 'rt',
    });
    // Otra aplicación: el token guardado no sirve.
    expect(() =>
      createMailSource(
        { mail: { ...cfg.mail, microsoft: { ...cfg.mail.microsoft, clientId: 'app-2' } } },
        env,
      ),
    ).toThrow(/mail login/);
  });
});

describe('inicio de sesión', () => {
  it('Google: navegador con PKCE y respuesta en un puerto local', async () => {
    const { googleLogin } = await import('../../src/infrastructure/mail/login.ts');
    const lines: string[] = [];
    let exchanged: URLSearchParams | null = null;
    const result = await googleLogin({
      clientId: 'cliente',
      clientSecret: 'secreto',
      tokenUrl: 'http://google.test/token',
      print: (l) => lines.push(l),
      openBrowser: (url) => {
        const auth = new URL(url);
        expect(auth.searchParams.get('scope')).toBe(
          'https://www.googleapis.com/auth/gmail.readonly',
        );
        expect(auth.searchParams.get('code_challenge_method')).toBe('S256');
        // Google redirige al puerto local con el código y el mismo state.
        const back = new URL(auth.searchParams.get('redirect_uri')!);
        back.searchParams.set('code', 'codigo-1');
        back.searchParams.set('state', auth.searchParams.get('state')!);
        void fetch(back);
      },
      fetchImpl: async (_url, init) => {
        exchanged = new URLSearchParams(String(init?.body));
        return json({ access_token: 'at', refresh_token: 'rt-nuevo', expires_in: 3600 });
      },
    });
    expect(result).toEqual({ refreshToken: 'rt-nuevo' });
    expect(exchanged!.get('code')).toBe('codigo-1');
    expect(exchanged!.get('code_verifier')).toMatch(/^[\w-]{43}$/);
    expect(lines[1]).toMatch(/^https:\/\/accounts\.google\.com\/o\/oauth2\/v2\/auth\?/);
  });

  it('Microsoft: código de dispositivo, esperando mientras el usuario inicia sesión', async () => {
    const { microsoftDeviceLogin } = await import('../../src/infrastructure/mail/login.ts');
    const replies = [
      json({
        device_code: 'dc',
        user_code: 'ABCD-1234',
        verification_uri: 'https://microsoft.com/devicelogin',
        interval: 1,
      }),
      json({ error: 'authorization_pending' }, 400),
      json({ error: 'slow_down' }, 400),
      json({ access_token: 'at', refresh_token: 'rt-ms' }),
    ];
    const urls: string[] = [];
    const waits: number[] = [];
    const lines: string[] = [];
    const result = await microsoftDeviceLogin({
      clientId: 'app',
      tenant: 'consumers',
      print: (l) => lines.push(l),
      sleep: async (ms) => waits.push(ms),
      fetchImpl: async (url) => {
        urls.push(String(url));
        return replies.shift()!;
      },
    });
    expect(result).toEqual({ refreshToken: 'rt-ms' });
    expect(lines[0]).toContain('ABCD-1234');
    expect(urls[0]).toBe('https://login.microsoftonline.com/consumers/oauth2/v2.0/devicecode');
    expect(waits).toEqual([1000, 1000, 6000]);
  });
});
