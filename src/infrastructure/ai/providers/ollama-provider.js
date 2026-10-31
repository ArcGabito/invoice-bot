import { z } from 'zod';
import { ExtractionError } from '../../../shared/errors.js';
import { ExtractionSchema } from '../extraction-schema.js';
import { SYSTEM_PROMPT, buildUserPrompt } from '../prompt.js';

/**
 * Proveedor local mediante la API HTTP de Ollama. Los documentos no salen
 * del equipo. Usa el parámetro `format` con un JSON Schema para forzar la
 * estructura, y valida la respuesta con el mismo esquema Zod.
 *
 * Decisiones tomadas tras probar con modelos locales reales:
 * - `think: false` por defecto: los modelos con razonamiento (qwen3, qwen3.5…)
 *   tardan minutos y pueden agotar el contexto antes de escribir el JSON.
 * - `num_ctx` explícito: el valor por defecto de Ollama (4096) se queda corto.
 * - Respuesta en streaming: `fetch` de Node corta a los 5 minutos si no
 *   recibe cabeceras; en streaming llegan enseguida y solo cuenta `timeoutMs`.
 */
export class OllamaProvider {
  name = 'ollama';

  constructor({
    url,
    model,
    think = false,
    numCtx = 8192,
    timeoutMs = 10 * 60 * 1000,
    fetchImpl = globalThis.fetch,
  }) {
    this.url = url.replace(/\/$/, '');
    this.model = model;
    this.think = think;
    this.numCtx = numCtx;
    this.timeoutMs = timeoutMs;
    this.fetch = fetchImpl;
    this.jsonSchema = z.toJSONSchema(ExtractionSchema);
  }

  async extract({ text, fileName }) {
    const body = {
      model: this.model,
      stream: true,
      think: this.think,
      format: this.jsonSchema,
      options: { temperature: 0, num_ctx: this.numCtx },
      messages: [
        { role: 'system', content: SYSTEM_PROMPT },
        { role: 'user', content: buildUserPrompt({ text, fileName }) },
      ],
    };

    let res = await this.post(body);
    if (res.status === 400) {
      const detail = await res.text().catch(() => '');
      // Modelos sin soporte de razonamiento rechazan el parámetro `think`.
      if (!/think/i.test(detail)) throw requestError(res.status, detail);
      delete body.think;
      res = await this.post(body);
    }
    if (!res.ok) throw requestError(res.status, await res.text().catch(() => ''));

    let reply;
    try {
      reply = parseChatResponse(await res.text());
    } catch (error) {
      throw new ExtractionError(`Respuesta de Ollama interrumpida: ${error.message}`, {
        kind: 'transient',
        retryable: true,
        cause: error,
      });
    }
    const { content, thinking, doneReason, usage, model } = reply;

    if (doneReason === 'length') {
      throw new ExtractionError(
        `La respuesta quedó truncada: aumenta INVOICEBOT_OLLAMA_NUM_CTX (ahora ${this.numCtx})`,
        { kind: 'invalid_output', retryable: false, rawResponse: content },
      );
    }
    if (content.trim() === '' && thinking) {
      throw new ExtractionError(
        'El modelo usó la respuesta solo para razonar; usa INVOICEBOT_OLLAMA_THINK=false',
        { kind: 'invalid_output', retryable: false, rawResponse: thinking.slice(0, 2000) },
      );
    }

    let json;
    try {
      json = JSON.parse(content);
    } catch {
      throw new ExtractionError('Ollama devolvió JSON inválido', {
        kind: 'invalid_output',
        retryable: true,
        rawResponse: content,
      });
    }
    const parsed = ExtractionSchema.safeParse(json);
    if (!parsed.success) {
      throw new ExtractionError(
        `La respuesta no cumple el esquema: ${parsed.error.issues[0]?.message}`,
        { kind: 'invalid_output', retryable: true, rawResponse: content },
      );
    }
    return { data: parsed.data, raw: content, model: model ?? this.model, usage };
  }

  async post(body) {
    try {
      return await this.fetch(`${this.url}/api/chat`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        signal: AbortSignal.timeout(this.timeoutMs),
        body: JSON.stringify(body),
      });
    } catch (error) {
      throw new ExtractionError(`No se pudo conectar con Ollama en ${this.url}: ${error.message}`, {
        kind: 'transient',
        retryable: true,
        cause: error,
      });
    }
  }

  async ping() {
    const res = await this.fetch(`${this.url}/api/tags`, { signal: AbortSignal.timeout(5000) });
    if (!res.ok) throw new Error(`Ollama respondió ${res.status}`);
    const { models = [] } = await res.json();
    const names = models.map((m) => m.name);
    if (!names.some((n) => n === this.model || n === `${this.model}:latest`)) {
      throw new Error(
        `El modelo ${this.model} no está descargado. Ejecuta: ollama pull ${this.model}` +
          (names.length ? ` (disponibles: ${names.join(', ')})` : ''),
      );
    }
    return `modelo ${this.model} disponible (razonamiento ${this.think ? 'activado' : 'desactivado'}, contexto ${this.numCtx})`;
  }
}

function requestError(status, detail) {
  const retryable = status >= 500 || status === 429;
  return new ExtractionError(`Ollama respondió ${status}: ${detail.slice(0, 200)}`, {
    kind: retryable ? 'transient' : 'provider_request',
    retryable,
  });
}

/**
 * Une los fragmentos NDJSON de /api/chat en streaming. También acepta una
 * respuesta única (stream: false), que es un solo objeto JSON.
 */
export function parseChatResponse(text) {
  let content = '';
  let thinking = '';
  let last = null;
  for (const line of text.split('\n')) {
    if (line.trim() === '') continue;
    const chunk = JSON.parse(line);
    if (chunk.error) throw new Error(chunk.error);
    content += chunk.message?.content ?? '';
    thinking += chunk.message?.thinking ?? '';
    last = chunk;
  }
  if (!last?.done) throw new Error('la respuesta terminó antes de completarse');
  return {
    content,
    thinking,
    doneReason: last.done_reason ?? null,
    model: last.model ?? null,
    usage: {
      input_tokens: last.prompt_eval_count ?? null,
      output_tokens: last.eval_count ?? null,
    },
  };
}
