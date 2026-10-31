import { z } from 'zod';
import { ExtractionError, errorMessage } from '../../../shared/errors.ts';
import type {
  AiExtractionResult,
  AiProvider,
  AiUsage,
} from '../../../application/ports/ai-provider.ts';
import { ExtractionSchema } from '../extraction-schema.ts';
import { SYSTEM_PROMPT, buildUserPrompt } from '../prompt.ts';

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
export class OllamaProvider implements AiProvider {
  readonly name = 'ollama';
  readonly url: string;
  readonly model: string;
  readonly think: boolean;
  readonly numCtx: number;
  readonly timeoutMs: number;
  readonly fetch: typeof globalThis.fetch;
  readonly jsonSchema: unknown;

  constructor({
    url,
    model,
    think = false,
    numCtx = 8192,
    timeoutMs = 10 * 60 * 1000,
    fetchImpl = globalThis.fetch,
  }: {
    url: string;
    model: string;
    think?: boolean;
    numCtx?: number;
    timeoutMs?: number;
    fetchImpl?: typeof globalThis.fetch;
  }) {
    this.url = url.replace(/\/$/, '');
    this.model = model;
    this.think = think;
    this.numCtx = numCtx;
    this.timeoutMs = timeoutMs;
    this.fetch = fetchImpl;
    this.jsonSchema = z.toJSONSchema(ExtractionSchema);
  }

  async extract({
    text,
    fileName,
    ocr = false,
  }: {
    text: string;
    fileName: string;
    ocr?: boolean;
  }): Promise<AiExtractionResult> {
    const body: Record<string, unknown> = {
      model: this.model,
      stream: true,
      think: this.think,
      format: this.jsonSchema,
      options: { temperature: 0, num_ctx: this.numCtx },
      messages: [
        { role: 'system', content: SYSTEM_PROMPT },
        { role: 'user', content: buildUserPrompt({ text, fileName, ocr }) },
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

    let reply: ChatReply;
    try {
      reply = parseChatResponse(await res.text());
    } catch (error) {
      throw new ExtractionError(`Respuesta de Ollama interrumpida: ${errorMessage(error)}`, {
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

    let json: unknown;
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

  async post(body: Record<string, unknown>): Promise<Response> {
    try {
      return await this.fetch(`${this.url}/api/chat`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        signal: AbortSignal.timeout(this.timeoutMs),
        body: JSON.stringify(body),
      });
    } catch (error) {
      throw new ExtractionError(
        `No se pudo conectar con Ollama en ${this.url}: ${errorMessage(error)}`,
        {
          kind: 'transient',
          retryable: true,
          cause: error,
        },
      );
    }
  }

  async ping(): Promise<string> {
    const res = await this.fetch(`${this.url}/api/tags`, { signal: AbortSignal.timeout(5000) });
    if (!res.ok) throw new Error(`Ollama respondió ${res.status}`);
    const { models = [] } = (await res.json()) as { models?: { name: string }[] };
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

function requestError(status: number, detail: string): ExtractionError {
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
interface ChatReply {
  content: string;
  thinking: string;
  doneReason: string | null;
  model: string | null;
  usage: AiUsage;
}

interface ChatChunk {
  error?: string;
  message?: { content?: string; thinking?: string };
  done?: boolean;
  done_reason?: string;
  model?: string;
  prompt_eval_count?: number;
  eval_count?: number;
}

export function parseChatResponse(text: string): ChatReply {
  let content = '';
  let thinking = '';
  let last: ChatChunk | null = null;
  for (const line of text.split('\n')) {
    if (line.trim() === '') continue;
    const chunk = JSON.parse(line) as ChatChunk;
    if (chunk.error) throw new Error(chunk.error);
    content += chunk.message?.content ?? '';
    thinking += chunk.message?.thinking ?? '';
    last = chunk;
  }
  if (!last?.done) throw new Error('la respuesta terminó antes de completarse');
  const end: ChatChunk = last;
  return {
    content,
    thinking,
    doneReason: end.done_reason ?? null,
    model: end.model ?? null,
    usage: {
      input_tokens: end.prompt_eval_count ?? null,
      output_tokens: end.eval_count ?? null,
    },
  };
}
