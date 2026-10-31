import {
  MIN_PASSWORD_LENGTH,
  addUser,
  resetPassword,
  setUserActive,
} from '../../application/use-cases/users.ts';
import type { UserWithPassword } from '../../application/use-cases/users.ts';
import { EXIT_CODES } from '../../shared/errors.ts';
import type { Command } from 'commander';
import type { Printer } from '../output.ts';
import type { Runner } from '../index.ts';

/** Lee la contraseña de la entrada estándar (para scripts): `... | invoicebot user add ana --password-stdin`. */
async function readPasswordFromStdin(): Promise<string> {
  const chunks: Buffer[] = [];
  for await (const chunk of process.stdin as AsyncIterable<Buffer>) chunks.push(chunk);
  return Buffer.concat(chunks)
    .toString('utf8')
    .replace(/\r?\n$/, '');
}

function printPassword(out: Printer, { user, password, generated }: UserWithPassword): void {
  if (!generated) return;
  out.line(`Contraseña de ${user.username}: ${password}`);
  out.line('Entrégasela por un canal seguro: no se vuelve a mostrar ni se puede recuperar.');
}

export function registerUsers(program: Command, run: Runner): void {
  const user = program
    .command('user')
    .description('Usuarios de la pantalla de revisión en red (serve en una VM o servidor)');

  user
    .command('add')
    .description('Da de alta a una persona; sin --password-stdin se genera una contraseña')
    .argument('<usuario>', 'nombre de usuario (p. ej. ana.perez)')
    .option('--name <nombre>', 'nombre completo que se muestra en la pantalla')
    .option(
      '--password-stdin',
      `lee la contraseña de la entrada estándar (mínimo ${MIN_PASSWORD_LENGTH} caracteres)`,
      false,
    )
    .action((username: string, options: { name?: string; passwordStdin: boolean }) =>
      run(async (ctx, out) => {
        const result = await addUser({
          users: ctx.users,
          username,
          displayName: options.name,
          password: options.passwordStdin ? await readPasswordFromStdin() : null,
        });
        out.data({ user: result.user, password: result.generated ? result.password : undefined });
        out.line(`Usuario ${result.user.username} (${result.user.display_name}) creado.`);
        printPassword(out, result);
        return EXIT_CODES.OK;
      }),
    );

  user
    .command('list')
    .description('Lista los usuarios y su último acceso')
    .action(() =>
      run(async (ctx, out) => {
        const rows = await ctx.users.list();
        out.data(rows);
        if (rows.length === 0)
          out.line('No hay usuarios. Crea uno con: invoicebot user add <usuario>');
        for (const u of rows) {
          out.line(
            `${u.active ? '✔' : '–'} ${u.username.padEnd(24)} ${u.display_name.padEnd(28)} último acceso: ${u.last_login_at ?? 'nunca'}`,
          );
        }
        return EXIT_CODES.OK;
      }),
    );

  user
    .command('reset-password')
    .description('Cambia la contraseña; sin --password-stdin se genera una nueva')
    .argument('<usuario>')
    .option('--password-stdin', 'lee la nueva contraseña de la entrada estándar', false)
    .action((username: string, options: { passwordStdin: boolean }) =>
      run(async (ctx, out) => {
        const result = await resetPassword({
          users: ctx.users,
          username,
          password: options.passwordStdin ? await readPasswordFromStdin() : null,
        });
        out.data({ user: result.user, password: result.generated ? result.password : undefined });
        out.line(
          `Contraseña de ${result.user.username} cambiada. Sus sesiones abiertas se cierran.`,
        );
        printPassword(out, result);
        return EXIT_CODES.OK;
      }),
    );

  const toggles: [string, boolean, string][] = [
    ['disable', false, 'Bloquea el acceso (el historial de revisiones se conserva)'],
    ['enable', true, 'Vuelve a permitir el acceso'],
  ];
  for (const [name, active, description] of toggles) {
    user
      .command(name)
      .description(description)
      .argument('<usuario>')
      .action((username: string) =>
        run(async (ctx, out) => {
          const updated = await setUserActive({ users: ctx.users, username, active });
          out.data(updated);
          out.line(`Usuario ${updated.username} ${active ? 'activado' : 'desactivado'}.`);
          return EXIT_CODES.OK;
        }),
      );
  }
}
