import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import tls from 'node:tls';
import { InvalidArgumentError } from 'commander';
import type { Command } from 'commander';
import type { ServerOptions } from 'node:https';
import { isLoopback, startReviewServer } from '../../web/review-server.ts';
import type { ReviewServerAuth } from '../../web/review-server.ts';
import { ConfigError, EXIT_CODES, errorMessage } from '../../shared/errors.ts';
import type { Config } from '../../shared/config.ts';
import type { Context } from '../context.ts';
import type { Runner } from '../index.ts';

function parsePort(value: string): number {
  const n = Number(value);
  if (!Number.isInteger(n) || n < 1024 || n > 65535) {
    throw new InvalidArgumentError('debe ser un puerto entre 1024 y 65535');
  }
  return n;
}

/** Abre la URL en el navegador predeterminado del sistema. */
function openBrowser(url: string): void {
  const [cmd, args]: [string, string[]] =
    process.platform === 'win32'
      ? ['cmd', ['/c', 'start', '""', url]]
      : process.platform === 'darwin'
        ? ['open', [url]]
        : ['xdg-open', [url]];
  spawn(cmd, args, { detached: true, stdio: 'ignore', windowsHide: true }).unref();
}

/** Lee el certificado configurado y comprueba que sirve antes de abrir el puerto. */
function loadTls(
  { certFile, keyFile, pfxFile }: Config['serve']['tls'],
  env: Record<string, string | undefined> = process.env,
): ServerOptions | null {
  if (!certFile && !pfxFile) return null;
  const read = (file: string | null, variable: string) => {
    try {
      return fs.readFileSync(file ?? '');
    } catch (error) {
      throw new ConfigError(`No se pudo leer ${variable} (${file}): ${errorMessage(error)}`);
    }
  };
  const options: ServerOptions = pfxFile
    ? {
        pfx: read(pfxFile, 'INVOICEBOT_SERVE_TLS_PFX'),
        passphrase: env.INVOICEBOT_SERVE_TLS_PASSPHRASE || undefined,
      }
    : {
        cert: read(certFile, 'INVOICEBOT_SERVE_TLS_CERT'),
        key: read(keyFile, 'INVOICEBOT_SERVE_TLS_KEY'),
        passphrase: env.INVOICEBOT_SERVE_TLS_PASSPHRASE || undefined,
      };
  try {
    tls.createSecureContext(options);
  } catch (error) {
    throw new ConfigError(
      `El certificado HTTPS no es válido o la contraseña (INVOICEBOT_SERVE_TLS_PASSPHRASE) no coincide: ${errorMessage(error)}`,
    );
  }
  return options;
}

/**
 * Comprueba lo que exige abrir la pantalla a la red: al menos un usuario
 * activo y HTTPS (o un proxy HTTPS delante, con INVOICEBOT_SERVE_ALLOW_HTTP).
 */
export async function prepareNetworkServe(
  ctx: Pick<Context, 'config' | 'users'>,
): Promise<{ auth: ReviewServerAuth; tls: ServerOptions | null }> {
  const { serve } = ctx.config;
  if ((await ctx.users.countActive()) === 0) {
    throw new ConfigError(
      'Para abrir la pantalla de revisión a la red crea antes al menos un usuario: invoicebot user add <usuario> --name "Nombre"',
    );
  }
  const tlsOptions = loadTls(serve.tls);
  if (!tlsOptions && !serve.allowHttp) {
    throw new ConfigError(
      'En red la pantalla de revisión necesita HTTPS: define INVOICEBOT_SERVE_TLS_PFX (o INVOICEBOT_SERVE_TLS_CERT y _KEY). ' +
        'Solo si un proxy HTTPS de la empresa va delante, usa INVOICEBOT_SERVE_ALLOW_HTTP=true.',
    );
  }
  return {
    auth: { users: ctx.users, sessionHours: serve.sessionHours },
    tls: tlsOptions,
  };
}

export function registerServe(program: Command, run: Runner): void {
  program
    .command('serve')
    .description(
      'Abre la pantalla de revisión (solo en este equipo, o en red con usuarios y HTTPS)',
    )
    .option('-p, --port <puerto>', 'puerto (por defecto: INVOICEBOT_SERVE_PORT o 4317)', parsePort)
    .option(
      '--host <dirección>',
      'dirección en la que escucha: 127.0.0.1 (solo este equipo) o 0.0.0.0 (red); por defecto INVOICEBOT_SERVE_HOST',
    )
    .option('--open', 'abre el navegador automáticamente', false)
    .action((options: { port?: number; host?: string; open: boolean }) =>
      run(async (ctx, out) => {
        const host = options.host ?? ctx.config.serve.host;
        const port = options.port ?? ctx.config.serve.port;
        const network = !isLoopback(host);
        const access = network ? await prepareNetworkServe(ctx) : { auth: null, tls: null };
        const server = await startReviewServer({
          deps: ctx,
          reviewer: ctx.reviewer,
          host,
          port,
          ...access,
        });

        if (network) {
          const scheme = access.tls ? 'https' : 'http';
          out.line(`Pantalla de revisión en red: ${scheme}://${os.hostname()}:${server.port}/`);
          out.line(
            `Cada persona entra con su usuario (invoicebot user list). Sesiones de ${ctx.config.serve.sessionHours} h.`,
          );
          if (!access.tls) {
            out.line(
              'Atención: HTTP sin cifrar (INVOICEBOT_SERVE_ALLOW_HTTP). Úsalo solo detrás de un proxy HTTPS.',
            );
          }
        } else {
          out.line(`Pantalla de revisión: ${server.url}`);
          out.line(`Revisando como: ${ctx.reviewer} (cámbialo con --user o INVOICEBOT_USER)`);
        }
        out.line('Ctrl+C para cerrar.');
        if (options.open) {
          openBrowser(
            network ? `${access.tls ? 'https' : 'http'}://localhost:${server.port}/` : server.url,
          );
        }

        await new Promise<void>((resolve) => {
          process.once('SIGINT', () => resolve());
          process.once('SIGTERM', () => resolve());
        });
        await server.close();
        out.line('Pantalla de revisión cerrada.');
        return EXIT_CODES.OK;
      }),
    );
}
