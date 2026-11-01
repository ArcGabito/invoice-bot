import { needsExchangeRate, isExchangeDependentRule } from '../../domain/exchange-rate.ts';
import type { InvoiceData } from '../../domain/invoice.ts';
import { exchangeDependentIssues } from '../../domain/invoice-validation.ts';
import type { InvoiceRepository } from '../ports/invoice-repository.ts';
import type { Deps } from '../deps.ts';

export interface ExchangeRateInput {
  currency: string;
  /** AAAA-MM-DD */
  date: string;
  buy: number | null;
  sell: number;
  source: 'sunat' | 'manual' | 'csv';
}

/** Tipo de cambio de la fecha de emisión; null si el comprobante está en soles o no lo hay. */
export async function lookupExchangeRate(
  repository: Pick<InvoiceRepository, 'findExchangeRate'>,
  invoice: Pick<InvoiceData, 'currency' | 'issue_date'>,
): Promise<{ rate: number; date: string } | null> {
  if (!needsExchangeRate(invoice) || !invoice.issue_date) return null;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(invoice.issue_date)) return null;
  return repository.findExchangeRate(invoice.currency!, invoice.issue_date);
}

/**
 * Aplica los tipos de cambio disponibles a las facturas en otra moneda que
 * aún no lo tenían, y recalcula sus incidencias de tipo de cambio y de
 * detracción (el monto de la detracción se comprueba en soles).
 */
export async function applyExchangeRates({
  deps,
}: {
  deps: Pick<Deps, 'repository' | 'logger'>;
}): Promise<{ updated: number; missing: number }> {
  const { repository, logger } = deps;
  let updated = 0;
  let missing = 0;
  for (const invoice of await repository.listInvoicesAwaitingExchangeRate()) {
    const rate = await lookupExchangeRate(repository, invoice);
    if (!rate) {
      missing++;
      continue;
    }
    await repository.transaction(async (tx) => {
      await tx.setInvoiceExchangeRate(invoice.id, rate);
      await tx.replaceRuleIssues(
        invoice.id,
        exchangeDependentIssues(invoice, rate),
        (code) => !isExchangeDependentRule(code),
      );
      await tx.recomputeValidationStatus(invoice.id);
    });
    updated++;
  }
  if (updated) logger.info({ updated, missing }, 'tipo de cambio aplicado a facturas pendientes');
  return { updated, missing };
}

/** Guarda tipos de cambio y los aplica a las facturas que los esperaban. */
export async function saveExchangeRates({
  rates,
  deps,
}: {
  rates: ExchangeRateInput[];
  deps: Pick<Deps, 'repository' | 'logger'>;
}): Promise<{ saved: number; updated: number; missing: number }> {
  const saved = await deps.repository.upsertExchangeRates(rates);
  const applied = await applyExchangeRates({ deps });
  return { saved, ...applied };
}
