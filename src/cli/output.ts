import { formatCents } from '../shared/money.ts';

export function formatDuration(ms: number): string {
  const total = Math.round(ms / 1000);
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(Math.floor(total / 3600))}:${pad(Math.floor((total % 3600) / 60))}:${pad(total % 60)}`;
}

export function money(
  currency: string | null | undefined,
  cents: number | null | undefined,
): string {
  return cents === null || cents === undefined
    ? '—'
    : `${currency ?? ''} ${formatCents(cents)}`.trim();
}

export interface InvoiceLabelFields {
  series: string | null;
  number: string | null;
  supplier_ruc: string | null;
  issue_date: string | null;
  currency: string | null;
  total_cents: number | null;
}

export function invoiceLabel(row: InvoiceLabelFields): string {
  const id = row.series && row.number ? `${row.series}-${row.number}` : 'sin serie/número';
  return `${id} · RUC ${row.supplier_ruc ?? '—'} · ${row.issue_date ?? 'sin fecha'} · ${money(row.currency, row.total_cents)}`;
}

export interface Printer {
  readonly json: boolean;
  line(text?: string): void;
  data(value: unknown): void;
  progress(text: string): void;
}

/** Imprime en stdout como JSON o como texto, según la opción global --json. */
export function createPrinter({ json = false }: { json?: boolean } = {}): Printer {
  return {
    json,
    line: (text = '') => {
      if (!json) process.stdout.write(`${text}\n`);
    },
    data: (value) => {
      if (json) process.stdout.write(`${JSON.stringify(value, null, 2)}\n`);
    },
    progress: (text) => {
      if (!json && process.stderr.isTTY) process.stderr.write(`${text}\n`);
    },
  };
}
