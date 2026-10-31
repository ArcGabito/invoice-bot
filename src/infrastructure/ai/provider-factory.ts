import { ConfigError } from '../../shared/errors.ts';
import { AnthropicProvider } from './providers/anthropic-provider.ts';
import { MockProvider } from './providers/mock-provider.ts';
import { OllamaProvider } from './providers/ollama-provider.ts';
import type { AiProvider } from '../../application/ports/ai-provider.ts';
import type { Config } from '../../shared/config.ts';

/** Crea el proveedor de IA configurado (puerto AiProvider). */
export function createAiProvider(config: Config): AiProvider {
  switch (config.aiProvider) {
    case 'anthropic':
      return new AnthropicProvider({
        model: config.anthropic.model,
        effort: config.anthropic.effort,
      });
    case 'ollama':
      return new OllamaProvider(config.ollama);
    case 'mock':
      return new MockProvider();
    default:
      throw new ConfigError(`Proveedor de IA desconocido: ${String(config.aiProvider)}`);
  }
}
