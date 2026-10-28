import { z } from 'zod';
import { ExtractionError } from '../../../shared/errors.js';
import { ExtractionSchema } from '../extraction-schema.js';
import { SYSTEM_PROMPT, buildUserPrompt } from '../prompt.js';

/**
 * Proveedor local mediante la API HTTP de Ollama. Los documentos no salen
 * del equipo. Usa el parámetro `format` con un JSON Schema para forzar la
 * estructura, y valida la respuesta con el mismo esquema Zod.
 */
export class OllamaProvider {
  name = 'ollama';

  constructor({ url, model, fetchImpl = globalThis.fetch, timeoutMs = 10 * 60 * 1000 }) {
    this.url = url.replace(/\/$/, '');
    this.model = model;
    this.fetch = fetchImpl;
    this.timeoutMs = timeoutMs;
    this.jsonSchema = z.toJSONSchema(ExtractionSchema);
  }

  async extract({ text, fileName }) {
    let res;
    try {
      res = await this.fetch(`${this.url}/api/chat`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        signal: AbortSignal.timeout(this.timeoutMs),
        body: JSON.stringify({
          model: this.model,
          stream: false,
          format: this.jsonSchema,
          options: { temperature: 0 },
          messages: [
            { role: 'system', content: SYSTEM_PROMPT },
            { role: 'user', content: buildUserPrompt({ text, fileName }) },
          ],
        }),
      });
    } catch (error) {
      throw new ExtractionError(`No se pudo conectar con Ollama en ${this.url}: ${error.message}`, {
        kind: 'transient',
        retryable: true,
        cause: error,
      });
    }
    if (!res.ok) {
      const body = await res.text().catch(() => '');
      const retryable = res.status >= 500 || res.status === 429;
      throw new ExtractionError(`Ollama respondió ${res.status}: ${body.slice(0, 200)}`, {
        kind: retryable ? 'transient' : 'provider_request',
        retryable,
      });
    }

    const payload = await res.json();
    const raw = payload?.message?.content ?? '';
    const usage = {
      input_tokens: payload.prompt_eval_count ?? null,
      output_tokens: payload.eval_count ?? null,
    };
    let json;
    try {
      json = JSON.parse(raw);
    } catch {
      throw new ExtractionError('Ollama devolvió JSON inválido', {
        kind: 'invalid_output',
        retryable: true,
        rawResponse: raw,
      });
    }
    const parsed = ExtractionSchema.safeParse(json);
    if (!parsed.success) {
      throw new ExtractionError(
        `La respuesta no cumple el esquema: ${parsed.error.issues[0]?.message}`,
        { kind: 'invalid_output', retryable: true, rawResponse: raw },
      );
    }
    return { data: parsed.data, raw, model: payload.model ?? this.model, usage };
  }

  async ping() {
    const res = await this.fetch(`${this.url}/api/tags`, { signal: AbortSignal.timeout(5000) });
    if (!res.ok) throw new Error(`Ollama respondió ${res.status}`);
    const { models = [] } = await res.json();
    const names = models.map((m) => m.name);
    if (!names.some((n) => n === this.model || n === `${this.model}:latest`)) {
      throw new Error(
        `El modelo ${this.model} no está descargado. Ejecuta: ollama pull ${this.model}`,
      );
    }
    return `modelo ${this.model} disponible`;
  }
}
