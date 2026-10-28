import fs from 'node:fs';
import { EXIT_CODES } from '../../shared/errors.js';

export function registerConfig(program, run) {
  program
    .command('config')
    .description('Muestra la configuración efectiva (sin revelar secretos)')
    .action(() =>
      run(async (ctx, out) => {
        const c = ctx.config;
        const view = {
          ...c,
          anthropic: {
            model: c.anthropic.model,
            effort: c.anthropic.effort,
            apiKey: c.anthropic.apiKeySet ? 'definida' : 'no definida',
          },
          amountTolerance: c.amountToleranceCents / 100,
        };
        delete view.amountToleranceCents;
        out.data(view);
        out.line(`Proveedor de IA:      ${c.aiProvider}`);
        if (c.aiProvider === 'anthropic') {
          out.line(`  Modelo:             ${c.anthropic.model} (effort ${c.anthropic.effort})`);
          out.line(`  ANTHROPIC_API_KEY:  ${view.anthropic.apiKey}`);
        }
        if (c.aiProvider === 'ollama')
          out.line(`  Ollama:             ${c.ollama.url} · ${c.ollama.model}`);
        out.line(`Directorio de datos:  ${c.dataDir}`);
        out.line(`Base de datos:        ${c.dbPath}`);
        out.line(`Exportaciones:        ${c.exportDir}`);
        out.line(`Concurrencia:         ${c.concurrency}`);
        out.line(`Intentos máximos:     ${c.maxAttempts}`);
        out.line(`Texto mínimo (OCR):   ${c.minTextChars} caracteres`);
        out.line(`Tolerancia importes:  ${view.amountTolerance}`);
        out.line(`Nivel de logs:        ${c.logLevel}`);
        return EXIT_CODES.OK;
      }),
    );
}

export function registerDoctor(program, run) {
  program
    .command('doctor')
    .description(
      'Comprueba Node.js, configuración, base de datos, lectura de PDF, OCR y proveedor de IA',
    )
    .option('--offline', 'no comprueba la conexión con el proveedor de IA', false)
    .action((options) =>
      run(async (ctx, out) => {
        const checks = [];
        const check = async (name, fn, { optional = false } = {}) => {
          try {
            checks.push({ name, ok: true, detail: (await fn()) ?? 'ok' });
          } catch (error) {
            checks.push({ name, ok: false, optional, detail: error.message });
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
        await check('SQLite', () => {
          const version = ctx.db.prepare('SELECT sqlite_version()').pluck().get();
          const migrations = ctx.db.prepare('SELECT COUNT(*) FROM schema_migrations').pluck().get();
          return `SQLite ${version}, ${migrations} migración(es) aplicadas`;
        });
        await check('Lectura de PDF', async () => {
          const pdfjs = await import('pdfjs-dist/legacy/build/pdf.mjs');
          return `pdfjs-dist ${pdfjs.version}`;
        });
        await check(
          'OCR',
          () => {
            throw new Error(
              'no disponible en esta versión (planificado para v0.2.0); los documentos escaneados quedan en estado needs_ocr',
            );
          },
          { optional: true },
        );
        if (!options.offline) {
          await check(`Proveedor de IA (${ctx.config.aiProvider})`, () => ctx.aiProvider.ping());
        }

        out.data(checks);
        for (const c of checks)
          out.line(`${c.ok ? '✔' : c.optional ? '–' : '✘'} ${c.name}: ${c.detail}`);
        return checks.some((c) => !c.ok && !c.optional) ? EXIT_CODES.GENERAL_ERROR : EXIT_CODES.OK;
      }),
    );
}
