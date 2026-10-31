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
    /** Razonamiento previo de los modelos que lo admiten: más lento y puede agotar el contexto. */
    think: z.boolean({ error: 'usa true o false' }),
    numCtx: z.int().min(2048).max(262_144),
    timeoutMs: z.int().min(10_000).max(3_600_000),
  }),
  sunat: z
    .object({
      /** Interruptor general: sin él, ninguna función consulta a SUNAT. */
      enabled: z.boolean({ error: 'usa true o false' }),
      /** Verificar automáticamente las facturas nuevas al terminar `scan`. */
      verifyOnScan: z.boolean({ error: 'usa true o false' }),
      /** RUC de la empresa que consulta (el de las credenciales de API SUNAT). */
      ruc: z
        .string()
        .regex(/^\d{11}$/, 'debe tener 11 dígitos')
        .nullable(),
      clientId: z.string().min(1).nullable(),
      clientSecretSet: z.boolean(),
      timeoutMs: z.int().min(1000).max(120_000),
      /** Pausa entre consultas consecutivas, para no saturar el servicio. */
      delayMs: z.int().min(0).max(60_000),
      /** null = URL oficial de SUNAT. Permite usar un proxy corporativo o un entorno de pruebas. */
      authUrl: z.url().nullable(),
      apiUrl: z.url().nullable(),
    })
    .refine((s) => s.enabled || !s.verifyOnScan, {
      message: 'INVOICEBOT_SUNAT_VERIFY_ON_SCAN requiere INVOICEBOT_SUNAT_ENABLED=true',
      path: ['verifyOnScan'],
    }),
  dataDir: z.string().min(1),
  dbPath: z.string().min(1),
  exportDir: z.string().min(1),
  exportFormat: z.enum(['csv', 'xlsx']),
  concurrency: z.int().min(1).max(16),
  maxAttempts: z.int().min(1).max(10),
  minTextChars: z.int().min(0),
  amountToleranceCents: z.int().min(0),
  logLevel: z.enum(LOG_LEVELS),
  /** Registros en archivo, uno por día. null = solo en pantalla (stderr). */
  log: z.object({
    dir: z.string().min(1).nullable(),
    retentionDays: z.int().min(1).max(3650),
  }),
  backup: z.object({
    dir: z.string().min(1),
    /** Copias que se conservan; las más antiguas se borran. */
    retention: z.int().min(1).max(1000),
    /** `run` hace una copia si la última es más antigua que este intervalo. */
    intervalHours: z
      .int()
      .min(1)
      .max(24 * 30),
  }),
  ocr: z.object({
    /** tesseract: local y sin internet · claude: visión de Claude (el documento sale del equipo) · none: desactivado. */
    engine: z.enum(['tesseract', 'claude', 'none']),
    languages: z
      .array(z.string().regex(/^[a-z_]{3,8}$/, 'código de idioma de Tesseract, p. ej. spa'))
      .min(1),
    languageDir: z.string().min(1),
    maxPages: z.int().min(1).max(20),
    scale: z.number().min(1).max(6),
    /** Por debajo de esta confianza (0-100) la factura queda con advertencia para revisión. */
    minConfidence: z.int().min(0).max(100),
  }),
  /**
   * Pantalla de revisión (`invoicebot serve`). Con host 127.0.0.1 solo se
   * usa desde este equipo; con otra dirección (p. ej. 0.0.0.0 en una VM) se
   * abre a la red y exige usuarios (`invoicebot user add`) y HTTPS.
   */
  serve: z
    .object({
      port: z.int().min(1024).max(65535),
      host: z.string().min(1),
      sessionHours: z
        .int()
        .min(1)
        .max(24 * 7),
      /** HTTP sin cifrar en red: solo detrás de un proxy HTTPS de la empresa. */
      allowHttp: z.boolean({ error: 'usa true o false' }),
      tls: z.object({
        certFile: z.string().min(1).nullable(),
        keyFile: z.string().min(1).nullable(),
        pfxFile: z.string().min(1).nullable(),
        passphraseSet: z.boolean(),
      }),
    })
    .refine((s) => Boolean(s.tls.certFile) === Boolean(s.tls.keyFile), {
      message: 'INVOICEBOT_SERVE_TLS_CERT e INVOICEBOT_SERVE_TLS_KEY van juntos',
      path: ['tls'],
    })
    .refine((s) => !(s.tls.pfxFile && s.tls.certFile), {
      message: 'usa INVOICEBOT_SERVE_TLS_PFX o el par CERT/KEY, no ambos',
      path: ['tls'],
    }),
  /** Ejecución desatendida (`invoicebot run`). */
  run: z.object({
    /** Carpeta de entrada; puede ser una carpeta de OneDrive o SharePoint sincronizada. */
    inboxDir: z.string().min(1).nullable(),
    recursive: z.boolean({ error: 'usa true o false' }),
    /** Exporta el mes actual y el anterior al terminar cada ejecución. */
    export: z.enum(['none', 'csv', 'xlsx']),
    intervalMinutes: z
      .int()
      .min(1)
      .max(24 * 60),
  }),
});

function intFrom(value, fallback) {
  if (value === undefined || value === '') return fallback;
  const n = Number(value);
  return Number.isInteger(n) ? n : Number.NaN;
}

/** "true"/"false" (también 1/0, sí/no). Un valor no reconocido se deja tal cual para que falle la validación. */
function boolFrom(value, fallback) {
  if (value === undefined || value === '') return fallback;
  const text = String(value).trim().toLowerCase();
  if (['true', '1', 'si', 'sí', 'yes'].includes(text)) return true;
  if (['false', '0', 'no'].includes(text)) return false;
  return value;
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
  const fileFrom = (value) => (value ? path.resolve(cwd, value) : null);
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
      model: env.INVOICEBOT_OLLAMA_MODEL || 'qwen3.5:4b',
      think: boolFrom(env.INVOICEBOT_OLLAMA_THINK, false),
      numCtx: intFrom(env.INVOICEBOT_OLLAMA_NUM_CTX, 8192),
      timeoutMs: intFrom(env.INVOICEBOT_OLLAMA_TIMEOUT_MS, 600_000),
    },
    sunat: {
      enabled: boolFrom(env.INVOICEBOT_SUNAT_ENABLED, false),
      verifyOnScan: boolFrom(env.INVOICEBOT_SUNAT_VERIFY_ON_SCAN, false),
      ruc: env.INVOICEBOT_SUNAT_RUC || null,
      clientId: env.INVOICEBOT_SUNAT_CLIENT_ID || null,
      clientSecretSet: Boolean(env.INVOICEBOT_SUNAT_CLIENT_SECRET),
      timeoutMs: intFrom(env.INVOICEBOT_SUNAT_TIMEOUT_MS, 20_000),
      delayMs: intFrom(env.INVOICEBOT_SUNAT_DELAY_MS, 0),
      authUrl: env.INVOICEBOT_SUNAT_AUTH_URL || null,
      apiUrl: env.INVOICEBOT_SUNAT_API_URL || null,
    },
    dataDir,
    dbPath: path.resolve(dataDir, overrides.dbFile ?? env.INVOICEBOT_DB_FILE ?? 'invoicebot.db'),
    exportDir: path.resolve(cwd, env.INVOICEBOT_EXPORT_DIR ?? './exports'),
    exportFormat: (env.INVOICEBOT_EXPORT_FORMAT || 'csv').toLowerCase(),
    concurrency: intFrom(overrides.concurrency ?? env.INVOICEBOT_CONCURRENCY, 2),
    maxAttempts: intFrom(env.INVOICEBOT_MAX_ATTEMPTS, 3),
    minTextChars: intFrom(env.INVOICEBOT_MIN_TEXT_CHARS, 40),
    amountToleranceCents: Number.isFinite(toleranceCents) ? toleranceCents : Number.NaN,
    logLevel: env.INVOICEBOT_LOG_LEVEL || 'warn',
    log: {
      dir: env.INVOICEBOT_LOG_DIR ? path.resolve(cwd, env.INVOICEBOT_LOG_DIR) : null,
      retentionDays: intFrom(env.INVOICEBOT_LOG_RETENTION_DAYS, 30),
    },
    backup: {
      dir: path.resolve(cwd, env.INVOICEBOT_BACKUP_DIR || path.join(dataDir, 'backups')),
      retention: intFrom(env.INVOICEBOT_BACKUP_RETENTION, 14),
      intervalHours: intFrom(env.INVOICEBOT_BACKUP_INTERVAL_HOURS, 24),
    },
    ocr: {
      engine: (env.INVOICEBOT_OCR_ENGINE || 'tesseract').toLowerCase(),
      languages: (env.INVOICEBOT_OCR_LANGUAGES || 'spa,eng').split(/[,+ ]+/).filter(Boolean),
      languageDir: path.join(dataDir, 'ocr'),
      maxPages: intFrom(env.INVOICEBOT_OCR_MAX_PAGES, 3),
      scale: env.INVOICEBOT_OCR_SCALE ? Number(env.INVOICEBOT_OCR_SCALE) : 3,
      minConfidence: intFrom(env.INVOICEBOT_OCR_MIN_CONFIDENCE, 70),
    },
    serve: {
      port: intFrom(env.INVOICEBOT_SERVE_PORT, 4317),
      host: env.INVOICEBOT_SERVE_HOST || '127.0.0.1',
      sessionHours: intFrom(env.INVOICEBOT_SERVE_SESSION_HOURS, 10),
      allowHttp: boolFrom(env.INVOICEBOT_SERVE_ALLOW_HTTP, false),
      tls: {
        certFile: fileFrom(env.INVOICEBOT_SERVE_TLS_CERT),
        keyFile: fileFrom(env.INVOICEBOT_SERVE_TLS_KEY),
        pfxFile: fileFrom(env.INVOICEBOT_SERVE_TLS_PFX),
        passphraseSet: Boolean(env.INVOICEBOT_SERVE_TLS_PASSPHRASE),
      },
    },
    run: {
      inboxDir: env.INVOICEBOT_INBOX_DIR ? path.resolve(cwd, env.INVOICEBOT_INBOX_DIR) : null,
      recursive: boolFrom(env.INVOICEBOT_INBOX_RECURSIVE, true),
      export: (env.INVOICEBOT_RUN_EXPORT || 'none').toLowerCase(),
      intervalMinutes: intFrom(env.INVOICEBOT_RUN_INTERVAL_MINUTES, 15),
    },
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
