import { formatCents } from '../shared/money.js';

export function formatDuration(ms) {
  const total = Math.round(ms / 1000);
  const pad = (n) => String(n).padStart(2, '0');
  return `${pad(Math.floor(total / 3600))}:${pad(Math.floor((total % 3600) / 60))}:${pad(total % 60)}`;
}

export function money(currency, cents) {
  return cents === null || cents === undefined
    ? '—'
    : `${currency ?? ''} ${formatCents(cents)}`.trim();
}

export function invoiceLabel(row) {
  const id = row.series && row.number ? `${row.series}-${row.number}` : 'sin serie/número';
  return `${id} · RUC ${row.supplier_ruc ?? '—'} · ${row.issue_date ?? 'sin fecha'} · ${money(row.currency, row.total_cents)}`;
}

/** Imprime en stdout como JSON o como texto, según la opción global --json. */
export function createPrinter({ json = false } = {}) {
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
