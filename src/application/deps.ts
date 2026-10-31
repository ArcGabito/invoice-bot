import type { Logger } from 'pino';
import type { Config } from '../shared/config.ts';
import type { AiProvider, VisionExtractor } from './ports/ai-provider.ts';
import type { DocumentExtractor } from './ports/document-extractor.ts';
import type { InvoiceRepository, UserRepository } from './ports/invoice-repository.ts';
import type { SunatClient } from '../infrastructure/sunat/sunat-client.ts';
import type { Db } from '../infrastructure/database/connection.ts';

/**
 * Dependencias de los casos de uso. La CLI las crea en src/cli/context.ts
 * (cada una al pedirla) y las pruebas en tests/helpers/context.ts.
 */
export interface Deps {
  config: Readonly<Config>;
  logger: Logger;
  db: Db;
  repository: InvoiceRepository;
  users: UserRepository;
  documentRouter: DocumentExtractor;
  aiProvider: AiProvider;
  /** Lectura con visión de Claude (solo con INVOICEBOT_OCR_ENGINE=claude). */
  visionExtractor: VisionExtractor | null;
  sunatClient: Pick<SunatClient, 'validateReceipt' | 'ping'>;
  /** Fecha local AAAA-MM-DD, para que la validación sea reproducible. */
  today: string;
  /** Quién firma las acciones de revisión. */
  reviewer?: string;
}
