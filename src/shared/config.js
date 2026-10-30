import path from 'node:path';
import { config as loadDotenv } from 'dotenv';
import { z } from 'zod';
import { ConfigError } from './errors.js';

const EFFORT_LEVELS = ['low', 'medium', 'high', 'xhigh', 'max'];
const LOG_LEVELS = ['trace', 'debug', 'info', 'warn', 'error', 'fatal', 'silent'];

const ConfigSchema = z.object({
  aiProvider: z.enum(['anthropic', 'ollama', 'mock']),
  anthropic: z.object({
    model: z.string().min(1),
    effort: z.enum(EFFORT_LEVELS),
    apiKeySet: z.boolean(),
  }),
  ollama: z.object({
    url: z.url(),
    model: z.string().min(1),
  }),
  sunat: z.object({
    /** RUC de la empresa que consulta (el de las credenciales de API SUNAT). */
    ruc: z
      .string()
      .regex(/^\d{11}$/, 'debe tener 11 dígitos')
      .nullable(),
    clientId: z.string().min(1).nullable(),
    clientSecretSet: z.boolean(),
  }),
  dataDir: z.string().min(1),
  dbPath: z.string().min(1),
  exportDir: z.string().min(1),
  concurrency: z.int().min(1).max(16),
  maxAttempts: z.int().min(1).max(10),
  minTextChars: z.int().min(0),
  amountToleranceCents: z.int().min(0),
  logLevel: z.enum(LOG_LEVELS),
});

function intFrom(value, fallback) {
  if (value === undefined || value === '') return fallback;
  const n = Number(value);
  return Number.isInteger(n) ? n : Number.NaN;
}

/**
 * Construye la configuración efectiva a partir de variables de entorno
 * (y de un archivo .env en el directorio actual, si existe). Los secretos
 * nunca se copian al objeto de configuración: solo se indica si están definidos.
 */
export function loadConfig({
  env = process.env,
  cwd = process.cwd(),
  loadEnvFile = true,
  overrides = {},
} = {}) {
  if (loadEnvFile) {
    loadDotenv({ path: path.join(cwd, '.env'), quiet: true, processEnv: env });
  }

  const dataDir = path.resolve(cwd, overrides.dataDir ?? env.INVOICEBOT_DATA_DIR ?? './data');
  const tolerance = env.INVOICEBOT_AMOUNT_TOLERANCE ?? '0.02';
  const toleranceCents = Math.round(Number(tolerance) * 100);

  const raw = {
    aiProvider: overrides.aiProvider ?? env.INVOICEBOT_AI_PROVIDER ?? 'anthropic',
    anthropic: {
      model: env.INVOICEBOT_ANTHROPIC_MODEL || 'claude-opus-5-5',
      effort: env.INVOICEBOT_ANTHROPIC_EFFORT || 'medium',
      apiKeySet: Boolean(env.ANTHROPIC_API_KEY || env.ANTHROPIC_AUTH_TOKEN),
    },
    ollama: {
      url: env.INVOICEBOT_OLLAMA_URL || 'http://127.0.0.1:11434',
      model: env.INVOICEBOT_OLLAMA_MODEL || 'qwen3:8b',
    },
    sunat: {
      ruc: env.INVOICEBOT_SUNAT_RUC || null,
      clientId: env.INVOICEBOT_SUNAT_CLIENT_ID || null,
      clientSecretSet: Boolean(env.INVOICEBOT_SUNAT_CLIENT_SECRET),
    },
    dataDir,
    dbPath: path.resolve(dataDir, overrides.dbFile ?? env.INVOICEBOT_DB_FILE ?? 'invoicebot.db'),
    exportDir: path.resolve(cwd, env.INVOICEBOT_EXPORT_DIR ?? './exports'),
    concurrency: intFrom(overrides.concurrency ?? env.INVOICEBOT_CONCURRENCY, 2),
    maxAttempts: intFrom(env.INVOICEBOT_MAX_ATTEMPTS, 3),
    minTextChars: intFrom(env.INVOICEBOT_MIN_TEXT_CHARS, 40),
    amountToleranceCents: Number.isFinite(toleranceCents) ? toleranceCents : Number.NaN,
    logLevel: env.INVOICEBOT_LOG_LEVEL || 'warn',
  };

  const result = ConfigSchema.safeParse(raw);
  if (!result.success) {
    const details = result.error.issues
      .map((issue) => `  - ${issue.path.join('.')}: ${issue.message}`)
      .join('\n');
    throw new ConfigError(`Configuración inválida:\n${details}`);
  }
  return Object.freeze(result.data);
}
