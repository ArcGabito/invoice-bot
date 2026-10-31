import fs from 'node:fs';
import type { Command } from 'commander';
import { sql } from 'kysely';
import { EXIT_CODES, errorMessage } from '../../shared/errors.ts';
import { isLoopback } from '../../web/review-server.ts';
import { prepareNetworkServe } from './serve.ts';
import { describeDatabase, schemaVersion } from '../../infrastructure/database/connection.ts';
import type { Runner } from '../index.ts';

export function registerConfig(program: Command, run: Runner): void {
  program
    .command('config')
    .description('Muestra la configuración efectiva (sin revelar secretos)')
    .action(() =>
      run(
        async (ctx, out) => {
          const c = ctx.config;
          const { amountToleranceCents, ...rest } = c;
          const view = {
            ...rest,
            anthropic: {
              model: c.anthropic.model,
              effort: c.anthropic.effort,
              apiKey: c.anthropic.apiKeySet ? 'definida' : 'no definida',
            },
            amountTolerance: amountToleranceCents / 100,
          };
          out.data(view);
          out.line(`Proveedor de IA:      ${c.aiProvider}`);
          if (c.aiProvider === 'anthropic') {
            out.line(`  Modelo:             ${c.anthropic.model} (effort ${c.anthropic.effort})`);
            out.line(`  ANTHROPIC_API_KEY:  ${view.anthropic.apiKey}`);
          }
          if (c.aiProvider === 'ollama')
            out.line(
              `  Ollama:             ${c.ollama.url} · ${c.ollama.model} · razonamiento ${c.ollama.think ? 'sí' : 'no'} · contexto ${c.ollama.numCtx}`,
            );
          const s = c.sunat;
          if (!s.enabled) {
            out.line('Verificación SUNAT:   desactivada (INVOICEBOT_SUNAT_ENABLED=false)');
          } else {
            out.line(
              `Verificación SUNAT:   activada${s.verifyOnScan ? ', automática al escanear' : ', manual (verify-sunat)'}`,
            );
            out.line(`  RUC consultante:    ${s.ruc ?? 'no definido'}`);
            out.line(
              `  Credenciales:       client_id ${s.clientId ? 'definido' : 'no definido'} · client_secret ${s.clientSecretSet ? 'definido' : 'no definido'}`,
            );
            out.line(
              `  Conexión:           espera máx. ${s.timeoutMs / 1000} s · pausa entre consultas ${s.delayMs} ms`,
            );
            if (s.authUrl || s.apiUrl) {
              out.line(
                `  URLs propias:       ${[s.authUrl, s.apiUrl].filter(Boolean).join(' · ')}`,
              );
            }
          }
          out.line(`Directorio de datos:  ${c.dataDir}`);
          out.line(
            `Base de datos:        SQL Server ${describeDatabase(c)} · usuario ${c.db.user ?? 'no definido'} · contraseña ${c.db.passwordSet ? 'definida' : 'no definida'} · cifrado ${c.db.encrypt ? 'sí' : 'no'}${c.db.trustServerCertificate ? ' (certificado sin verificar)' : ''}`,
          );
          out.line(
            `Copias de seguridad:  ${c.backup.dir ?? 'carpeta por defecto de SQL Server'} · se conservan ${c.backup.retention} · cada ${c.backup.intervalHours} h`,
          );
          out.line(`Exportaciones:        ${c.exportDir}`);
          out.line(`Concurrencia:         ${c.concurrency}`);
          out.line(`Intentos máximos:     ${c.maxAttempts}`);
          out.line(`Texto mínimo (OCR):   ${c.minTextChars} caracteres`);
          out.line(
            `OCR:                  ${
              c.ocr.engine === 'none'
                ? 'desactivado'
                : c.ocr.engine === 'claude'
                  ? 'visión de Claude (el documento sale del equipo)'
                  : `Tesseract local · idiomas ${c.ocr.languages.join('+')} · confianza mínima ${c.ocr.minConfidence} %`
            }`,
          );
          const sv = c.serve;
          if (isLoopback(sv.host)) {
            out.line(`Pantalla de revisión: solo este equipo · puerto ${sv.port}`);
          } else {
            const tlsMode = sv.tls.pfxFile
              ? `HTTPS (${sv.tls.pfxFile})`
              : sv.tls.certFile
                ? `HTTPS (${sv.tls.certFile})`
                : sv.allowHttp
                  ? 'HTTP sin cifrar (proxy HTTPS delante)'
                  : 'falta el certificado HTTPS';
            out.line(
              `Pantalla de revisión: en red (${sv.host}:${sv.port}) · ${tlsMode} · sesiones de ${sv.sessionHours} h`,
            );
          }
          out.line(`Tolerancia importes:  ${view.amountTolerance}`);
          out.line(`Nivel de logs:        ${c.logLevel}`);
          return EXIT_CODES.OK;
        },
        { database: false },
      ),
    );
}

interface Check {
  name: string;
  ok: boolean;
  optional?: boolean;
  detail: string;
}

export function registerDoctor(program: Command, run: Runner): void {
  program
    .command('doctor')
    .description(
      'Comprueba Node.js, configuración, base de datos, lectura de PDF, OCR y proveedor de IA',
    )
    .option('--offline', 'no comprueba la conexión con el proveedor de IA', false)
    .action((options: { offline: boolean }) =>
      run(
        async (ctx, out) => {
          const checks: Check[] = [];
          const check = async (
            name: string,
            fn: () => unknown,
            { optional = false }: { optional?: boolean } = {},
          ): Promise<boolean> => {
            try {
              const detail = await fn();
              checks.push({ name, ok: true, detail: typeof detail === 'string' ? detail : 'ok' });
              return true;
            } catch (error) {
              checks.push({ name, ok: false, optional, detail: errorMessage(error) });
              return false;
            }
          };

          await check('Node.js', () => {
            const major = Number(process.versions.node.split('.')[0]);
            if (major < 24)
              throw new Error(`se requiere Node.js 24 LTS o superior (actual: ${process.version})`);
            return process.version;
          });
          await check('Directorio de datos', () => {
            fs.mkdirSync(ctx.config.dataDir, { recursive: true });
            fs.accessSync(ctx.config.dataDir, fs.constants.W_OK);
            return ctx.config.dataDir;
          });
          const dbOk = await check(`SQL Server (${describeDatabase(ctx.config)})`, async () => {
            await ctx.connect();
            const info = await sql<{ version: string; edition: string }>`
            SELECT CAST(SERVERPROPERTY('ProductVersion') AS NVARCHAR(128)) AS version,
              CAST(SERVERPROPERTY('Edition') AS NVARCHAR(128)) AS edition`.execute(ctx.db);
            const row = info.rows[0];
            return `SQL Server ${row?.version} (${row?.edition}), esquema versión ${await schemaVersion(ctx.db)}`;
          });
          await check('Lectura de PDF', async () => {
            const pdfjs = await import('pdfjs-dist/legacy/build/pdf.mjs');
            return `pdfjs-dist ${pdfjs.version}`;
          });
          const { engine } = ctx.config.ocr;
          if (engine === 'none') {
            await check(
              'OCR',
              () => {
                throw new Error(
                  'desactivado (INVOICEBOT_OCR_ENGINE=none): los escaneos quedan en needs_ocr',
                );
              },
              { optional: true },
            );
          } else if (engine === 'tesseract') {
            await check('OCR (Tesseract, local)', () => {
              const engine = (
                ctx.documentRouter as { ocrEngine?: { ping(): Promise<string> } | null }
              ).ocrEngine;
              if (!engine) throw new Error('motor de OCR no disponible');
              return engine.ping();
            });
          } else if (!options.offline) {
            await check('OCR (visión de Claude)', async () => {
              if (!ctx.visionExtractor) throw new Error('lectura con visión no disponible');
              const detail = await ctx.visionExtractor.ping();
              return `${detail}; los documentos sin texto se envían a Anthropic`;
            });
          }
          if (!isLoopback(ctx.config.serve.host) && dbOk) {
            await check('Pantalla de revisión en red', async () => {
              const { tls } = await prepareNetworkServe(ctx);
              const n = await ctx.users.countActive();
              return `${n} usuario(s) activo(s), ${tls ? 'certificado HTTPS válido' : 'HTTP detrás de un proxy'}`;
            });
          }
          if (!options.offline) {
            await check(`Proveedor de IA (${ctx.config.aiProvider})`, () => ctx.aiProvider.ping());
          }
          if (!ctx.config.sunat.enabled) {
            await check(
              'API SUNAT',
              () => {
                throw new Error('desactivada (opcional; activa con INVOICEBOT_SUNAT_ENABLED=true)');
              },
              { optional: true },
            );
          } else if (!options.offline) {
            await check('API SUNAT', () => ctx.sunatClient.ping());
          }

          out.data(checks);
          for (const c of checks)
            out.line(`${c.ok ? '✔' : c.optional ? '–' : '✘'} ${c.name}: ${c.detail}`);
          return checks.some((c) => !c.ok && !c.optional)
            ? EXIT_CODES.GENERAL_ERROR
            : EXIT_CODES.OK;
        },
        { database: false },
      ),
    );
}
