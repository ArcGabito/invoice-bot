import { spawn } from 'node:child_process';
import { InvalidArgumentError } from 'commander';
import { startReviewServer } from '../../web/review-server.js';
import { EXIT_CODES } from '../../shared/errors.js';

function parsePort(value) {
  const n = Number(value);
  if (!Number.isInteger(n) || n < 1024 || n > 65535) {
    throw new InvalidArgumentError('debe ser un puerto entre 1024 y 65535');
  }
  return n;
}

/** Abre la URL en el navegador predeterminado del sistema. */
function openBrowser(url) {
  const [cmd, args] =
    process.platform === 'win32'
      ? ['cmd', ['/c', 'start', '""', url]]
      : process.platform === 'darwin'
        ? ['open', [url]]
        : ['xdg-open', [url]];
  spawn(cmd, args, { detached: true, stdio: 'ignore', windowsHide: true }).unref();
}

export function registerServe(program, run) {
  program
    .command('serve')
    .description('Abre la pantalla de revisión en el navegador (solo accesible desde este equipo)')
    .option(
      '-p, --port <puerto>',
      'puerto local (por defecto: INVOICEBOT_SERVE_PORT o 4317)',
      parsePort,
    )
    .option('--open', 'abre el navegador automáticamente', false)
    .action((options) =>
      run(async (ctx, out) => {
        const server = await startReviewServer({
          deps: ctx,
          reviewer: ctx.reviewer,
          port: options.port ?? ctx.config.serve.port,
        });
        out.line(`Pantalla de revisión: ${server.url}`);
        out.line(`Revisando como: ${ctx.reviewer} (cámbialo con --user o INVOICEBOT_USER)`);
        out.line('Ctrl+C para cerrar.');
        if (options.open) openBrowser(server.url);

        await new Promise((resolve) => {
          process.once('SIGINT', resolve);
          process.once('SIGTERM', resolve);
        });
        await server.close();
        out.line('Pantalla de revisión cerrada.');
        return EXIT_CODES.OK;
      }),
    );
}
