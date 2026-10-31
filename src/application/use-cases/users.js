import { UserError } from '../../shared/errors.js';
import {
  dummyVerify,
  generatePassword,
  hashPassword,
  verifyPassword,
} from '../../infrastructure/security/password.js';

export const MIN_PASSWORD_LENGTH = 12;
const MAX_PASSWORD_LENGTH = 256;
const USERNAME = /^[a-z0-9][a-z0-9._-]{1,39}$/;

/** Usuarios en minúsculas: "Ana" y "ana" son la misma persona. */
export function normalizeUsername(username) {
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

function checkPassword(password) {
  if (typeof password !== 'string' || password.length < MIN_PASSWORD_LENGTH) {
    throw new UserError(`La contraseña debe tener al menos ${MIN_PASSWORD_LENGTH} caracteres`);
  }
  if (password.length > MAX_PASSWORD_LENGTH)
    throw new UserError('La contraseña es demasiado larga');
  return password;
}

function requireUser(users, username) {
  const user = users.findByUsername(normalizeUsername(username));
  if (!user) throw new UserError(`No existe el usuario "${username}"`);
  return user;
}

/**
 * Da de alta a una persona. Sin contraseña se genera una aleatoria, que se
 * devuelve una sola vez para entregársela.
 * @returns {Promise<{ user: object, password: string, generated: boolean }>}
 */
export async function addUser({ users, username, displayName, password = null }) {
  const name = normalizeUsername(username);
  if (users.findByUsername(name)) throw new UserError(`El usuario "${name}" ya existe`);
  const generated = password === null;
  const secret = generated ? generatePassword() : checkPassword(password);
  const id = users.insert({
    username: name,
    displayName: String(displayName ?? '').trim() || name,
    passwordHash: await hashPassword(secret),
  });
  return { user: users.getById(id), password: secret, generated };
}

export async function resetPassword({ users, username, password = null }) {
  const user = requireUser(users, username);
  const generated = password === null;
  const secret = generated ? generatePassword() : checkPassword(password);
  users.setPassword(user.id, await hashPassword(secret));
  return { user: users.getById(user.id), password: secret, generated };
}

export function setUserActive({ users, username, active }) {
  const user = requireUser(users, username);
  users.setActive(user.id, active);
  return users.getById(user.id);
}

/**
 * Comprueba usuario y contraseña. Devuelve el usuario (sin hash) o null, y
 * tarda lo mismo si el usuario no existe, para no revelar cuáles existen.
 */
export async function authenticate({ users, username, password }) {
  let name;
  try {
    name = normalizeUsername(username);
  } catch {
    await dummyVerify(String(password ?? ''));
    return null;
  }
  const user = users.findByUsername(name);
  if (!user || typeof password !== 'string' || password.length > MAX_PASSWORD_LENGTH) {
    await dummyVerify(String(password ?? ''));
    return null;
  }
  if (!(await verifyPassword(password, user.password_hash)) || !user.active) return null;
  users.touchLogin(user.id);
  return users.getById(user.id);
}
