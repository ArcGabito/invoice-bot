import fs from 'node:fs';
import type { Config } from '../../shared/config.ts';
import { ConfigError } from '../../shared/errors.ts';
import { GMAIL_SCOPE, createGmailSource } from './gmail-source.ts';
import {
  GRAPH_APP_SCOPE,
  GRAPH_DELEGATED_SCOPE,
  createGraphSource,
  microsoftTokenUrl,
} from './graph-source.ts';
import { createImapSource, defaultImapHost } from './imap-source.ts';
import type { MailSource } from './mail-source.ts';
import {
  clientCredentialsProvider,
  googleServiceAccountProvider,
  refreshTokenProvider,
} from './oauth.ts';
import type { GoogleServiceAccountKey } from './oauth.ts';
import { readMailToken, writeMailToken } from './token-store.ts';
import type { Fetch } from './http.ts';

export const GOOGLE_TOKEN_URL = 'https://oauth2.googleapis.com/token';

type Env = Record<string, string | undefined>;

/** Puntos de conexión alternativos, para pruebas con servidores simulados. */
export interface MailEndpoints {
  gmailApiUrl?: string;
  googleTokenUrl?: string;
  graphApiUrl?: string;
  microsoftLoginUrl?: string;
  fetchImpl?: Fetch;
}

function needs(values: [string, unknown][], what: string): void {
  const missing = values.filter(([, v]) => !v).map(([name]) => name);
  if (missing.length) throw new ConfigError(`${what}: falta ${missing.join(', ')} en .env`);
}

/**
 * Refresh token del usuario: el que guardó `invoicebot mail login` (y que se
 * renueva solo) o, si no hay, INVOICEBOT_MAIL_REFRESH_TOKEN.
 */
function storedRefreshToken(
  mail: Config['mail'],
  provider: 'gmail' | 'graph',
  clientId: string,
  env: Env,
): string {
  const stored = readMailToken(mail.tokenFile);
  if (stored && stored.provider === provider && stored.clientId === clientId) {
    return stored.refreshToken;
  }
  const fromEnv = env.INVOICEBOT_MAIL_REFRESH_TOKEN;
  if (fromEnv) return fromEnv;
  throw new ConfigError(
    'Falta el acceso al buzón: inicia sesión una vez con `invoicebot mail login` ' +
      '(o define INVOICEBOT_MAIL_REFRESH_TOKEN en .env).',
  );
}

/**
 * Crea el conector de correo configurado en .env (INVOICEBOT_MAIL_PROVIDER).
 * Comprueba que estén todos los datos antes de conectar.
 */
export function createMailSource(
  config: Pick<Config, 'mail'>,
  env: Env = process.env,
  endpoints: MailEndpoints = {},
): MailSource {
  const { mail } = config;
  const { fetchImpl } = endpoints;
  switch (mail.provider) {
    case 'none':
      throw new ConfigError(
        'La lectura de correo está desactivada. Define INVOICEBOT_MAIL_PROVIDER (imap, gmail o graph) en .env.',
      );

    case 'imap': {
      const host = mail.imap.host ?? defaultImapHost(mail.user);
      needs(
        [
          ['INVOICEBOT_MAIL_USER', mail.user],
          ['INVOICEBOT_MAIL_PASSWORD', mail.passwordSet],
          ['INVOICEBOT_MAIL_IMAP_HOST', host],
        ],
        'Correo IMAP',
      );
      return createImapSource({
        host: host!,
        port: mail.imap.port,
        tls: mail.imap.tls,
        user: mail.user!,
        password: env.INVOICEBOT_MAIL_PASSWORD ?? '',
        folder: mail.folder,
        timeoutMs: mail.timeoutMs,
      });
    }

    case 'gmail': {
      const common = {
        label: mail.folder,
        timeoutMs: mail.timeoutMs,
        fetchImpl,
        ...(endpoints.gmailApiUrl && { apiUrl: endpoints.gmailApiUrl }),
      };
      // Google Workspace: cuenta de servicio con delegación de dominio, sin inicio de sesión.
      if (mail.google.serviceAccountFile) {
        needs([['INVOICEBOT_MAIL_USER', mail.user]], 'Gmail con cuenta de servicio');
        let key: GoogleServiceAccountKey;
        try {
          key = JSON.parse(fs.readFileSync(mail.google.serviceAccountFile, 'utf8'));
        } catch (error) {
          throw new ConfigError(
            `No se pudo leer la clave de la cuenta de servicio ${mail.google.serviceAccountFile}: ${(error as Error).message}`,
          );
        }
        if (endpoints.googleTokenUrl) key = { ...key, token_uri: endpoints.googleTokenUrl };
        return createGmailSource({
          ...common,
          userId: mail.user!,
          tokens: googleServiceAccountProvider({
            key,
            subject: mail.user!,
            scope: GMAIL_SCOPE,
            fetchImpl,
            timeoutMs: mail.timeoutMs,
          }),
        });
      }
      const { clientId } = mail.google;
      needs(
        [
          ['INVOICEBOT_MAIL_GOOGLE_CLIENT_ID', clientId],
          ['INVOICEBOT_MAIL_GOOGLE_CLIENT_SECRET', mail.google.clientSecretSet],
        ],
        'Gmail',
      );
      return createGmailSource({
        ...common,
        tokens: refreshTokenProvider({
          tokenUrl: endpoints.googleTokenUrl ?? GOOGLE_TOKEN_URL,
          clientId: clientId!,
          clientSecret: env.INVOICEBOT_MAIL_GOOGLE_CLIENT_SECRET,
          refreshToken: storedRefreshToken(mail, 'gmail', clientId!, env),
          fetchImpl,
          timeoutMs: mail.timeoutMs,
        }),
      });
    }

    case 'graph': {
      const { clientId, tenant, clientSecretSet } = mail.microsoft;
      needs([['INVOICEBOT_MAIL_MS_CLIENT_ID', clientId]], 'Microsoft 365 / Outlook');
      const tokenUrl = microsoftTokenUrl(tenant, endpoints.microsoftLoginUrl);
      const common = {
        folder: mail.folder,
        timeoutMs: mail.timeoutMs,
        fetchImpl,
        ...(endpoints.graphApiUrl && { apiUrl: endpoints.graphApiUrl }),
      };
      // Microsoft 365 de empresa: permiso de aplicación otorgado por el administrador.
      if (clientSecretSet) {
        needs([['INVOICEBOT_MAIL_USER', mail.user]], 'Microsoft 365 con permiso de aplicación');
        if (['common', 'organizations', 'consumers'].includes(tenant.toLowerCase())) {
          throw new ConfigError(
            'Con INVOICEBOT_MAIL_MS_CLIENT_SECRET (permiso de aplicación) define INVOICEBOT_MAIL_MS_TENANT con el ID del directorio de la empresa.',
          );
        }
        return createGraphSource({
          ...common,
          mailbox: mail.user,
          tokens: clientCredentialsProvider({
            tokenUrl,
            clientId: clientId!,
            clientSecret: env.INVOICEBOT_MAIL_MS_CLIENT_SECRET ?? '',
            scope: GRAPH_APP_SCOPE,
            fetchImpl,
            timeoutMs: mail.timeoutMs,
          }),
        });
      }
      // Inicio de sesión del usuario (Outlook.com o una cuenta de Microsoft 365).
      const stored = readMailToken(mail.tokenFile);
      return createGraphSource({
        ...common,
        tokens: refreshTokenProvider({
          tokenUrl,
          clientId: clientId!,
          refreshToken: storedRefreshToken(mail, 'graph', clientId!, env),
          scope: GRAPH_DELEGATED_SCOPE,
          // Microsoft renueva el refresh token en cada uso: se guarda el nuevo.
          onRotate: (refreshToken) =>
            writeMailToken(mail.tokenFile, {
              provider: 'graph',
              clientId: clientId!,
              account: stored?.account ?? '',
              refreshToken,
            }),
          fetchImpl,
          timeoutMs: mail.timeoutMs,
        }),
      });
    }
  }
}
