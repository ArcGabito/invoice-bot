import fs from 'node:fs';
import { afterAll, describe, expect, it } from 'vitest';
import {
  addUser,
  authenticate,
  normalizeUsername,
  resetPassword,
  setUserActive,
} from '../../src/application/use-cases/users.ts';
import { UserRepository } from '../../src/infrastructure/database/repositories/user-repository.ts';
import {
  generatePassword,
  hashPassword,
  verifyPassword,
} from '../../src/infrastructure/security/password.ts';
import { UserError } from '../../src/shared/errors.ts';
import { makeTestDeps } from '../helpers/context.ts';

const env = await makeTestDeps();
const users = new UserRepository(env.db);
afterAll(async () => {
  fs.rmSync(env.dir, { recursive: true, force: true });
});

describe('contraseñas', () => {
  it('guarda un hash scrypt con sal y lo verifica', async () => {
    const hash = await hashPassword('una contraseña larga');
    expect(hash).toMatch(/^scrypt\$32768\$8\$1\$[^$]+\$[^$]+$/);
    expect(hash).not.toContain('contraseña');
    expect(await hashPassword('una contraseña larga')).not.toBe(hash);
    expect(await verifyPassword('una contraseña larga', hash)).toBe(true);
    expect(await verifyPassword('otra contraseña', hash)).toBe(false);
    expect(await verifyPassword('x', 'texto-plano')).toBe(false);
  });

  it('genera contraseñas legibles y distintas', async () => {
    const a = generatePassword();
    expect(a).toMatch(/^[a-zA-Z2-9]{5}(-[a-zA-Z2-9]{5}){3}$/);
    expect(a).not.toMatch(/[01lIO]/);
    expect(generatePassword()).not.toBe(a);
  });
});

describe('usuarios', () => {
  it('normaliza el nombre y rechaza nombres raros', async () => {
    expect(normalizeUsername('  Ana.Perez ')).toBe('ana.perez');
    expect(() => normalizeUsername('josé')).toThrow(UserError);
    expect(() => normalizeUsername('a')).toThrow(UserError);
    expect(() => normalizeUsername('ana perez')).toThrow(UserError);
  });

  it('da de alta con contraseña generada o propia, sin repetir usuarios', async () => {
    const generated = await addUser({ users, username: 'Ana', displayName: 'Ana Pérez' });
    expect(generated).toMatchObject({
      generated: true,
      user: { username: 'ana', display_name: 'Ana Pérez', active: true },
    });
    expect(generated.user).not.toHaveProperty('password_hash');

    const own = await addUser({ users, username: 'luis', password: 'clave-de-luis-2026' });
    expect(own).toMatchObject({ generated: false, user: { display_name: 'luis' } });

    await expect(addUser({ users, username: 'ANA' })).rejects.toThrow(/ya existe/);
    await expect(addUser({ users, username: 'eva', password: 'corta' })).rejects.toThrow(
      /al menos 12/,
    );
  });

  it('autentica sin distinguir mayúsculas en el usuario y registra el acceso', async () => {
    const user = await authenticate({ users, username: 'LUIS', password: 'clave-de-luis-2026' });
    expect(user).toMatchObject({ username: 'luis' });
    expect(user!.last_login_at).toBeTruthy();
    expect(await authenticate({ users, username: 'luis', password: 'otra-clave-123' })).toBeNull();
    expect(
      await authenticate({ users, username: 'nadie', password: 'clave-de-luis-2026' }),
    ).toBeNull();
    expect(await authenticate({ users, username: 'josé', password: 'x' })).toBeNull();
    expect(await authenticate({ users, username: 'luis', password: undefined })).toBeNull();
  });

  it('una persona desactivada no entra y al reactivarla vuelve a entrar', async () => {
    await setUserActive({ users, username: 'luis', active: false });
    expect(
      await authenticate({ users, username: 'luis', password: 'clave-de-luis-2026' }),
    ).toBeNull();
    await setUserActive({ users, username: 'luis', active: true });
    expect(
      await authenticate({ users, username: 'luis', password: 'clave-de-luis-2026' }),
    ).not.toBeNull();
    await expect(setUserActive({ users, username: 'nadie', active: true })).rejects.toThrow(
      /No existe/,
    );
  });

  it('al cambiar la contraseña la anterior deja de servir', async () => {
    const { password } = await resetPassword({ users, username: 'luis' });
    expect(
      await authenticate({ users, username: 'luis', password: 'clave-de-luis-2026' }),
    ).toBeNull();
    expect(await authenticate({ users, username: 'luis', password })).not.toBeNull();
  });
});
