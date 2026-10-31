import crypto from 'node:crypto';
import { promisify } from 'node:util';

const scrypt = promisify(crypto.scrypt);

/** Parámetros de scrypt: ~64 MB y unas decenas de milisegundos por intento. */
const PARAMS = { N: 2 ** 15, r: 8, p: 1 };
const KEY_LENGTH = 32;
const maxmem = (N, r) => 128 * N * r * 2;

/** Caracteres sin ambigüedad (sin 0/O, 1/l/I) para contraseñas que se dictan o se copian. */
const ALPHABET = 'abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789';

/** Contraseña aleatoria de 20 caracteres en grupos de cinco (~115 bits). */
export function generatePassword() {
  const chars = Array.from({ length: 20 }, () => ALPHABET[crypto.randomInt(ALPHABET.length)]);
  return chars.join('').match(/.{5}/g).join('-');
}

export async function hashPassword(password) {
  const { N, r, p } = PARAMS;
  const salt = crypto.randomBytes(16);
  const hash = await scrypt(password.normalize('NFKC'), salt, KEY_LENGTH, {
    N,
    r,
    p,
    maxmem: maxmem(N, r),
  });
  return ['scrypt', N, r, p, salt.toString('base64'), hash.toString('base64')].join('$');
}

/** Compara en tiempo constante. Un hash mal formado nunca coincide. */
export async function verifyPassword(password, stored) {
  const [scheme, N, r, p, salt, expected] = String(stored).split('$');
  if (scheme !== 'scrypt' || !salt || !expected) return false;
  const expectedBuffer = Buffer.from(expected, 'base64');
  const actual = await scrypt(
    String(password).normalize('NFKC'),
    Buffer.from(salt, 'base64'),
    expectedBuffer.length,
    { N: Number(N), r: Number(r), p: Number(p), maxmem: maxmem(Number(N), Number(r)) },
  );
  return crypto.timingSafeEqual(actual, expectedBuffer);
}

/** Hash de relleno: verificar contra él cuesta lo mismo que contra uno real. */
let dummyHash;
export async function dummyVerify(password) {
  dummyHash ??= hashPassword(generatePassword());
  await verifyPassword(password, await dummyHash);
}
