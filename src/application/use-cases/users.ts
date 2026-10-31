import { UserError } from '../../shared/errors.ts';
import {
  dummyVerify,
  generatePassword,
  hashPassword,
  verifyPassword,
} from '../../infrastructure/security/password.ts';
import type { UserRepository } from '../ports/invoice-repository.ts';
import type { PublicUser } from '../../infrastructure/database/repositories/user-repository.ts';

export const MIN_PASSWORD_LENGTH = 12;
const MAX_PASSWORD_LENGTH = 256;
const USERNAME = /^[a-z0-9][a-z0-9._-]{1,39}$/;

/** Usuarios en minúsculas: "Ana" y "ana" son la misma persona. */
export function normalizeUsername(username: unknown): string {
  const name = String(username ?? '')
    .trim()
    .toLowerCase();
  if (!USERNAME.test(name)) {
    throw new UserError(
      'El usuario debe tener de 2 a 40 caracteres: letras sin tildes, números, punto, guion o guion bajo',
    );
  }
  return name;
}

function checkPassword(password: unknown): string {
  if (typeof password !== 'string' || password.length < MIN_PASSWORD_LENGTH) {
    throw new UserError(`La contraseña debe tener al menos ${MIN_PASSWORD_LENGTH} caracteres`);
  }
  if (password.length > MAX_PASSWORD_LENGTH)
    throw new UserError('La contraseña es demasiado larga');
  return password;
}

async function requireUser(users: UserRepository, username: string) {
  const user = await users.findByUsername(normalizeUsername(username));
  if (!user) throw new UserError(`No existe el usuario "${username}"`);
  return user;
}

async function publicUser(users: UserRepository, id: number): Promise<PublicUser> {
  const user = await users.getById(id);
  if (!user) throw new UserError(`No existe el usuario #${id}`);
  return user;
}

export interface UserWithPassword {
  user: PublicUser;
  /** Se muestra una sola vez para entregársela a la persona. */
  password: string;
  generated: boolean;
}

/**
 * Da de alta a una persona. Sin contraseña se genera una aleatoria, que se
 * devuelve una sola vez para entregársela.
 */
export async function addUser({
  users,
  username,
  displayName,
  password = null,
}: {
  users: UserRepository;
  username: string;
  displayName?: string | null;
  password?: string | null;
}): Promise<UserWithPassword> {
  const name = normalizeUsername(username);
  if (await users.findByUsername(name)) throw new UserError(`El usuario "${name}" ya existe`);
  const generated = password === null;
  const secret = generated ? generatePassword() : checkPassword(password);
  const id = await users.insert({
    username: name,
    displayName: String(displayName ?? '').trim() || name,
    passwordHash: await hashPassword(secret),
  });
  return { user: await publicUser(users, id), password: secret, generated };
}

export async function resetPassword({
  users,
  username,
  password = null,
}: {
  users: UserRepository;
  username: string;
  password?: string | null;
}): Promise<UserWithPassword> {
  const user = await requireUser(users, username);
  const generated = password === null;
  const secret = generated ? generatePassword() : checkPassword(password);
  await users.setPassword(user.id, await hashPassword(secret));
  return { user: await publicUser(users, user.id), password: secret, generated };
}

export async function setUserActive({
  users,
  username,
  active,
}: {
  users: UserRepository;
  username: string;
  active: boolean;
}): Promise<PublicUser> {
  const user = await requireUser(users, username);
  await users.setActive(user.id, active);
  return publicUser(users, user.id);
}

/**
 * Comprueba usuario y contraseña. Devuelve el usuario (sin hash) o null, y
 * tarda lo mismo si el usuario no existe, para no revelar cuáles existen.
 */
export async function authenticate({
  users,
  username,
  password,
}: {
  users: UserRepository;
  username: unknown;
  password: unknown;
}): Promise<PublicUser | null> {
  let name: string;
  try {
    name = normalizeUsername(username);
  } catch {
    await dummyVerify(String(password ?? ''));
    return null;
  }
  const user = await users.findByUsername(name);
  if (!user || typeof password !== 'string' || password.length > MAX_PASSWORD_LENGTH) {
    await dummyVerify(String(password ?? ''));
    return null;
  }
  if (!(await verifyPassword(password, user.password_hash)) || !user.active) return null;
  await users.touchLogin(user.id);
  return users.getById(user.id);
}
