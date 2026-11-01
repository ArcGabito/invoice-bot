import path from 'node:path';
import type { Command } from 'commander';
import { fetchMail } from '../../application/use-cases/fetch-mail.ts';
import type { MailSummary } from '../../application/use-cases/fetch-mail.ts';
import { readMailToken, writeMailToken } from '../../infrastructure/mail/token-store.ts';
import type { Config } from '../../shared/config.ts';
import { ConfigError, EXIT_CODES } from '../../shared/errors.ts';
import type { Printer } from '../output.ts';
import type { Runner } from '../index.ts';
import { openBrowser } from './serve.ts';

/** Los conectores (imapflow, mailparser) se cargan al usarlos: no retrasan el arranque de la CLI. */
export async function loadMailSource(config: Config) {
  const { createMailSource } = await import('../../infrastructure/mail/mail-factory.ts');
  return createMailSource(config);
}

/** Carpeta donde se guardan los adjuntos: INVOICEBOT_MAIL_DIR o "correo" dentro de la carpeta de entrada. */
export function mailDir(config: Pick<Config, 'run' | 'mail'>, inbox?: string): string {
  if (config.mail.dir && !inbox) return config.mail.dir;
  const inboxDir = inbox ? path.resolve(inbox) : config.run.inboxDir;
  if (!inboxDir) {
    throw new ConfigError('Define la carpeta de entrada con INVOICEBOT_INBOX_DIR o --inbox <ruta>');
  }
  return path.join(inboxDir, 'correo');
}

export function printMailSummary(
  out: Printer,
  mail: MailSummary | { error: string },
  indent = '',
): void {
  if ('error' in mail) {
    out.line(`${indent}Correo: no se pudo leer (${mail.error})`);
    return;
  }
  out.line(
    `${indent}Correo ${mail.account}: ${mail.read} mensajes nuevos · ${mail.files} archivos guardados` +
      (mail.noAttachments ? ` · ${mail.noAttachments} sin comprobantes` : '') +
      (mail.failed ? ` · ${mail.failed} con error` : '') +
      (mail.remaining ? ` · ${mail.remaining} quedan para el siguiente ciclo` : ''),
  );
}

export function registerMail(program: Command, run: Runner): void {
  const mail = program
    .command('mail')
    .description('Lectura de comprobantes desde un buzón de correo (IMAP, Gmail o Microsoft 365)');

  mail
    .command('login')
    .description('Inicia sesión una vez en Gmail u Outlook / Microsoft 365 y guarda el acceso')
    .action(() =>
      run(
        async (ctx, out) => {
          const { mail: cfg } = ctx.config;
          const print = (line: string) => out.line(line);
          let provider: 'gmail' | 'graph';
          let clientId: string;
          let refreshToken: string;
          if (cfg.provider === 'gmail' && !cfg.google.serviceAccountFile) {
            if (!cfg.google.clientId || !cfg.google.clientSecretSet) {
              throw new ConfigError(
                'Define INVOICEBOT_MAIL_GOOGLE_CLIENT_ID e INVOICEBOT_MAIL_GOOGLE_CLIENT_SECRET en .env',
              );
            }
            provider = 'gmail';
            clientId = cfg.google.clientId;
            const { googleLogin } = await import('../../infrastructure/mail/login.ts');
            const { GOOGLE_TOKEN_URL } = await import('../../infrastructure/mail/mail-factory.ts');
            ({ refreshToken } = await googleLogin({
              clientId,
              clientSecret: process.env.INVOICEBOT_MAIL_GOOGLE_CLIENT_SECRET ?? '',
              tokenUrl: GOOGLE_TOKEN_URL,
              openBrowser,
              print,
            }));
          } else if (cfg.provider === 'graph' && !cfg.microsoft.clientSecretSet) {
            if (!cfg.microsoft.clientId) {
              throw new ConfigError('Define INVOICEBOT_MAIL_MS_CLIENT_ID en .env');
            }
            provider = 'graph';
            clientId = cfg.microsoft.clientId;
            const { microsoftDeviceLogin } = await import('../../infrastructure/mail/login.ts');
            ({ refreshToken } = await microsoftDeviceLogin({
              clientId,
              tenant: cfg.microsoft.tenant,
              print,
            }));
          } else {
            throw new ConfigError(
              cfg.provider === 'imap'
                ? 'IMAP no necesita iniciar sesión: usa INVOICEBOT_MAIL_USER e INVOICEBOT_MAIL_PASSWORD.'
                : cfg.provider === 'none'
                  ? 'Define INVOICEBOT_MAIL_PROVIDER=gmail o graph en .env'
                  : 'Con cuenta de servicio o permiso de aplicación no hace falta iniciar sesión: usa `invoicebot mail check`.',
            );
          }
          writeMailToken(cfg.tokenFile, { provider, clientId, account: '', refreshToken });
          // Comprueba el acceso y anota la dirección del buzón.
          const source = await loadMailSource(ctx.config);
          const account = await source.connect().finally(() => source.close());
          const saved = readMailToken(cfg.tokenFile);
          writeMailToken(cfg.tokenFile, {
            provider,
            clientId,
            account,
            refreshToken: saved?.refreshToken ?? refreshToken,
          });
          out.data({ provider, account, tokenFile: cfg.tokenFile });
          out.line(`Sesión iniciada: ${account}. Acceso guardado en ${cfg.tokenFile}`);
          out.line('Protege ese archivo: permite leer el correo de esa cuenta.');
          return EXIT_CODES.OK;
        },
        { database: false },
      ),
    );

  mail
    .command('check')
    .description('Comprueba la conexión con el buzón y cuenta los correos con adjuntos')
    .action(() =>
      run(
        async (ctx, out) => {
          const { mail: cfg } = ctx.config;
          const source = await loadMailSource(ctx.config);
          try {
            const account = await source.connect();
            const since = new Date(Date.now() - cfg.sinceDays * 24 * 60 * 60 * 1000);
            const messages = await source.listMessages({ since });
            out.data({ provider: source.provider, account, messages: messages.length });
            out.line(`Conectado a ${account} (${source.provider}).`);
            out.line(
              `Correos con adjuntos en los últimos ${cfg.sinceDays} días: ${messages.length}`,
            );
          } finally {
            await source.close();
          }
          return EXIT_CODES.OK;
        },
        { database: false },
      ),
    );

  mail
    .command('fetch')
    .description('Descarga los comprobantes adjuntos de los correos nuevos a la carpeta de entrada')
    .option('--inbox <ruta>', 'carpeta de entrada (por defecto: INVOICEBOT_INBOX_DIR)')
    .action((options: { inbox?: string }) =>
      run(async (ctx, out) => {
        const dir = mailDir(ctx.config, options.inbox);
        const summary = await fetchMail({
          deps: ctx,
          source: await loadMailSource(ctx.config),
          dir,
        });
        out.data(summary);
        printMailSummary(out, summary);
        if (summary.files) out.line(`Guardados en ${dir}. Procésalos con: invoicebot scan`);
        return summary.failed ? EXIT_CODES.PARTIAL : EXIT_CODES.OK;
      }),
    );

  mail
    .command('list')
    .description('Últimos correos leídos y qué se hizo con cada uno')
    .option('-n, --limit <n>', 'cantidad', '20')
    .action((options: { limit: string }) =>
      run(async (ctx, out) => {
        const rows = await ctx.repository.listMailMessages({ limit: Number(options.limit) || 20 });
        out.data(rows);
        if (rows.length === 0) {
          out.line('Todavía no se ha leído ningún correo.');
          return EXIT_CODES.OK;
        }
        const label = {
          saved: 'guardado',
          no_attachments: 'sin comprobantes',
          failed: 'con error',
        };
        for (const r of rows) {
          out.line(
            `${(r.received_at ?? r.created_at).slice(0, 16).replace('T', ' ')}  ${label[r.status as keyof typeof label] ?? r.status}` +
              `${r.files ? ` (${r.files} archivos)` : ''}  ${r.sender ?? '—'}  ${r.subject ?? ''}`,
          );
          if (r.error_message) out.line(`    ${r.error_message}`);
        }
        return EXIT_CODES.OK;
      }),
    );
}
