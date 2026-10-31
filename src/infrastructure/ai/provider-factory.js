import { ConfigError } from '../../shared/errors.js';
import { AnthropicProvider } from './providers/anthropic-provider.js';
import { MockProvider } from './providers/mock-provider.js';
import { OllamaProvider } from './providers/ollama-provider.js';

/** Crea el proveedor de IA configurado (puerto AiProvider). */
export function createAiProvider(config) {
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
      throw new ConfigError(`Proveedor de IA desconocido: ${config.aiProvider}`);
  }
}
