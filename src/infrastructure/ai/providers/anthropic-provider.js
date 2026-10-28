import Anthropic from '@anthropic-ai/sdk';
import { betaZodOutputFormat } from '@anthropic-ai/sdk/helpers/beta/zod';
import { ExtractionError } from '../../../shared/errors.js';
import { ExtractionSchema } from '../extraction-schema.js';
import { SYSTEM_PROMPT, buildUserPrompt } from '../prompt.js';

/**
 * Proveedor Claude (Anthropic) con salida estructurada validada por Zod.
 *
 * Usa el fallback del lado del servidor: si el modelo rechaza una petición
 * por sus salvaguardas, la API la reintenta con otro modelo adecuado dentro
 * de la misma llamada. El modelo que realmente respondió se guarda en el run.
 */
export class AnthropicProvider {
  name = 'anthropic';

  constructor({ model, effort, client = new Anthropic({ maxRetries: 3 }) }) {
    this.model = model;
    this.effort = effort;
    this.client = client;
  }

  async extract({ text, fileName }) {
    let response;
    try {
      response = await this.client.beta.messages.parse({
        model: this.model,
        max_tokens: 16000,
        betas: ['server-side-fallback-2026-07-01'],
        fallbacks: 'default',
        system: SYSTEM_PROMPT,
        output_config: {
          effort: this.effort,
          format: betaZodOutputFormat(ExtractionSchema),
        },
        messages: [{ role: 'user', content: buildUserPrompt({ text, fileName }) }],
      });
    } catch (error) {
      throw toExtractionError(error);
    }

    const raw = response.content
      .filter((b) => b.type === 'text')
      .map((b) => b.text)
      .join('');
    const usage = {
      input_tokens: response.usage?.input_tokens ?? null,
      output_tokens: response.usage?.output_tokens ?? null,
    };
    const meta = { raw, model: response.model, usage };

    if (response.stop_reason === 'refusal') {
      const category = response.stop_details?.category ?? 'sin categoría';
      throw new ExtractionError(`El modelo rechazó el documento (${category})`, {
        kind: 'refused',
        retryable: false,
        rawResponse: raw,
      });
    }
    if (response.stop_reason === 'max_tokens') {
      throw new ExtractionError('La respuesta del modelo quedó truncada', {
        kind: 'invalid_output',
        retryable: true,
        rawResponse: raw,
      });
    }
    if (!response.parsed_output) {
      throw new ExtractionError('La respuesta no cumple el esquema de extracción', {
        kind: 'invalid_output',
        retryable: true,
        rawResponse: raw,
      });
    }
    return { data: response.parsed_output, ...meta };
  }

  /** Comprueba credenciales y modelo sin consumir tokens. */
  async ping() {
    try {
      const info = await this.client.models.retrieve(this.model);
      return `modelo ${info.id} disponible`;
    } catch (error) {
      if (error instanceof Anthropic.APIError) throw toExtractionError(error);
      // Sin credenciales el SDK falla antes de hacer la petición.
      throw new Error(
        `${error.message}\n  Define ANTHROPIC_API_KEY en .env, ejecuta \`ant auth login\` o usa --provider ollama`,
        { cause: error },
      );
    }
  }
}

function toExtractionError(error) {
  if (
    error instanceof Anthropic.AuthenticationError ||
    error instanceof Anthropic.PermissionDeniedError
  ) {
    return new ExtractionError(`Credenciales de Anthropic inválidas: ${error.message}`, {
      kind: 'provider_auth',
      retryable: false,
      cause: error,
    });
  }
  if (error instanceof Anthropic.NotFoundError) {
    return new ExtractionError(`Modelo no encontrado: ${error.message}`, {
      kind: 'provider_config',
      retryable: false,
      cause: error,
    });
  }
  if (error instanceof Anthropic.BadRequestError) {
    return new ExtractionError(`Petición rechazada por la API: ${error.message}`, {
      kind: 'provider_request',
      retryable: false,
      cause: error,
    });
  }
  if (error instanceof Anthropic.RateLimitError || error instanceof Anthropic.InternalServerError) {
    return new ExtractionError(`Error temporal de la API (${error.status}): ${error.message}`, {
      kind: 'transient',
      retryable: true,
      cause: error,
    });
  }
  if (error instanceof Anthropic.APIConnectionError) {
    return new ExtractionError(`No se pudo conectar con Anthropic: ${error.message}`, {
      kind: 'transient',
      retryable: true,
      cause: error,
    });
  }
  if (error instanceof Anthropic.APIError) {
    return new ExtractionError(`Error de la API (${error.status}): ${error.message}`, {
      kind: 'transient',
      retryable: true,
      cause: error,
    });
  }
  // Errores del SDK al interpretar la salida (JSON inválido, esquema no cumplido).
  if (error instanceof Anthropic.AnthropicError) {
    return new ExtractionError(`Salida del modelo no interpretable: ${error.message}`, {
      kind: 'invalid_output',
      retryable: true,
      cause: error,
    });
  }
  return error;
}
