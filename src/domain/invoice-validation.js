import { applyRateBasisPoints, formatCents, sumCents } from '../shared/money.js';
import { DOCUMENT_TYPES, SEVERITY, VALIDATION_STATUS, AMOUNT_FIELDS } from './invoice.js';
import { validateRuc } from './ruc-validator.js';

/** Versión de las reglas. Se guarda con cada factura para poder auditar resultados. */
export const VALIDATION_RULES_VERSION = '2026.10.1';

/**
 * Tasas de IGV admitidas, en puntos básicos: 18 % general (16 % IGV + 2 % IPM)
 * y 10,5 % del régimen especial para restaurantes y alojamientos (Ley 31556).
 */
export const DEFAULT_IGV_RATES_BP = Object.freeze([1800, 1050]);

const KNOWN_CURRENCIES = new Set(['PEN', 'USD', 'EUR']);
const REQUIRED_FIELDS = [
  'supplier_ruc',
  'document_type',
  'series',
  'number',
  'issue_date',
  'currency',
  'total_cents',
];

function issue(rule_code, severity, message, { expected = null, actual = null } = {}) {
  return {
    rule_code,
    severity,
    message,
    expected_value: expected === null ? null : String(expected),
    actual_value: actual === null ? null : String(actual),
  };
}

function isCalendarDate(text) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(text)) return false;
  const [y, m, d] = text.split('-').map(Number);
  const date = new Date(Date.UTC(y, m - 1, d));
  return date.getUTCFullYear() === y && date.getUTCMonth() === m - 1 && date.getUTCDate() === d;
}

function checkRequired(invoice) {
  return REQUIRED_FIELDS.filter(
    (field) => invoice[field] === null || invoice[field] === undefined,
  ).map((field) =>
    issue(
      'REQUIRED_FIELD_MISSING',
      SEVERITY.ERROR,
      `Falta el campo obligatorio ${field.replace(/_cents$/, '')}`,
      {
        actual: 'null',
      },
    ),
  );
}

function checkRucs(invoice) {
  const issues = [];
  const checks = [
    ['supplier_ruc', 'SUPPLIER_RUC', SEVERITY.ERROR, 'emisor'],
    ['customer_ruc', 'CUSTOMER_RUC', SEVERITY.WARNING, 'cliente'],
  ];
  for (const [field, prefix, severity, label] of checks) {
    const ruc = invoice[field];
    if (!ruc) continue;
    // Un DNI (8 dígitos) es válido como documento del cliente en boletas.
    if (field === 'customer_ruc' && /^\d{8}$/.test(ruc)) continue;
    const result = validateRuc(ruc);
    if (result.valid) continue;
    if (result.reason === 'check_digit') {
      issues.push(
        issue(
          `${prefix}_CHECK_DIGIT`,
          severity,
          `El dígito de control del RUC del ${label} no es correcto`,
          {
            expected: result.expected,
            actual: ruc[10],
          },
        ),
      );
    } else {
      issues.push(
        issue(
          `${prefix}_FORMAT`,
          severity,
          `El RUC del ${label} debe tener 11 dígitos y un prefijo válido (10, 15, 16, 17, 20)`,
          {
            actual: ruc,
          },
        ),
      );
    }
  }
  return issues;
}

function checkDates(invoice, today) {
  const issues = [];
  const { issue_date, due_date } = invoice;
  if (issue_date) {
    if (!isCalendarDate(issue_date)) {
      issues.push(
        issue(
          'ISSUE_DATE_INVALID',
          SEVERITY.ERROR,
          'La fecha de emisión no es una fecha válida (AAAA-MM-DD)',
          { actual: issue_date },
        ),
      );
    } else if (issue_date > today) {
      issues.push(
        issue('ISSUE_DATE_FUTURE', SEVERITY.WARNING, 'La fecha de emisión es posterior a hoy', {
          expected: `<= ${today}`,
          actual: issue_date,
        }),
      );
    } else if (issue_date < '2000-01-01') {
      issues.push(
        issue(
          'ISSUE_DATE_TOO_OLD',
          SEVERITY.WARNING,
          'La fecha de emisión es anterior al año 2000',
          { actual: issue_date },
        ),
      );
    }
  }
  if (due_date) {
    if (!isCalendarDate(due_date)) {
      issues.push(
        issue(
          'DUE_DATE_INVALID',
          SEVERITY.WARNING,
          'La fecha de vencimiento no es una fecha válida',
          { actual: due_date },
        ),
      );
    } else if (issue_date && isCalendarDate(issue_date) && due_date < issue_date) {
      issues.push(
        issue(
          'DUE_DATE_BEFORE_ISSUE',
          SEVERITY.WARNING,
          'La fecha de vencimiento es anterior a la de emisión',
          { expected: `>= ${issue_date}`, actual: due_date },
        ),
      );
    }
  }
  return issues;
}

function checkIdentification(invoice) {
  const issues = [];
  const { document_type, series, number, currency, supplier_name } = invoice;

  if (!supplier_name) {
    issues.push(
      issue(
        'SUPPLIER_NAME_MISSING',
        SEVERITY.WARNING,
        'No se identificó la razón social del emisor',
      ),
    );
  }
  if (document_type && !DOCUMENT_TYPES[document_type]) {
    issues.push(
      issue(
        'DOCUMENT_TYPE_UNKNOWN',
        SEVERITY.WARNING,
        'Tipo de comprobante fuera del catálogo soportado',
        { expected: Object.keys(DOCUMENT_TYPES).join(','), actual: document_type },
      ),
    );
  }
  if (series) {
    // Electrónica: letra + 3 alfanuméricos (F001, B001, E001). Física: 4 dígitos (0001).
    if (!/^[FBE][A-Z0-9]{3}$/.test(series) && !/^\d{4}$/.test(series)) {
      issues.push(
        issue(
          'SERIES_FORMAT',
          SEVERITY.WARNING,
          'La serie no sigue el formato SUNAT (F001, B001, E001 o 0001)',
          { actual: series },
        ),
      );
    } else if (document_type === '01' && series.startsWith('B')) {
      issues.push(
        issue(
          'SERIES_TYPE_MISMATCH',
          SEVERITY.WARNING,
          'Una factura no debería usar una serie de boleta (B)',
          { actual: series },
        ),
      );
    } else if (document_type === '03' && series.startsWith('F')) {
      issues.push(
        issue(
          'SERIES_TYPE_MISMATCH',
          SEVERITY.WARNING,
          'Una boleta no debería usar una serie de factura (F)',
          { actual: series },
        ),
      );
    }
  }
  if (number && !/^\d{1,8}$/.test(number)) {
    issues.push(
      issue('NUMBER_FORMAT', SEVERITY.WARNING, 'El correlativo debe tener entre 1 y 8 dígitos', {
        actual: number,
      }),
    );
  }
  if (currency) {
    if (!/^[A-Z]{3}$/.test(currency)) {
      issues.push(
        issue(
          'CURRENCY_INVALID',
          SEVERITY.ERROR,
          'La moneda debe ser un código ISO 4217 de 3 letras',
          { actual: currency },
        ),
      );
    } else if (!KNOWN_CURRENCIES.has(currency)) {
      issues.push(
        issue(
          'CURRENCY_UNUSUAL',
          SEVERITY.WARNING,
          'Moneda poco habitual para comprobantes peruanos',
          { expected: [...KNOWN_CURRENCIES].join(','), actual: currency },
        ),
      );
    }
  }
  return issues;
}

function checkAmounts(invoice, normalizationIssues, toleranceCents, igvRatesBp) {
  const issues = [];
  for (const { field, value } of normalizationIssues) {
    issues.push(
      issue('AMOUNT_UNPARSEABLE', SEVERITY.ERROR, `No se pudo interpretar el importe ${field}`, {
        actual: value,
      }),
    );
  }
  for (const field of AMOUNT_FIELDS) {
    const cents = invoice[`${field}_cents`];
    if (cents !== null && cents < 0) {
      issues.push(
        issue('AMOUNT_NEGATIVE', SEVERITY.ERROR, `El importe ${field} es negativo`, {
          expected: '>= 0',
          actual: formatCents(cents),
        }),
      );
    }
  }

  const {
    subtotal_cents: subtotal,
    taxable_amount_cents: taxable,
    exempt_amount_cents: exempt,
    unaffected_amount_cents: unaffected,
    discount_amount_cents: discount,
    igv_cents: igv,
    total_cents: total,
  } = invoice;

  if (total === 0) {
    issues.push(
      issue('TOTAL_ZERO', SEVERITY.WARNING, 'El importe total es cero (¿operación gratuita?)'),
    );
  }

  // Base imponible: el desglose tributario si existe; si no, el subtotal.
  const hasBreakdown = [taxable, exempt, unaffected].some((v) => v !== null);
  const base = hasBreakdown ? sumCents(taxable, exempt, unaffected) : subtotal;
  const charges = sumCents(
    igv,
    invoice.isc_cents,
    invoice.icbper_cents,
    invoice.other_charges_cents,
    invoice.other_taxes_cents,
  );

  if (total !== null && total > 0) {
    if (base === null) {
      issues.push(
        issue(
          'TOTAL_NOT_VERIFIABLE',
          SEVERITY.WARNING,
          'No hay subtotal ni desglose tributario para comprobar el total',
        ),
      );
    } else {
      const expected = base + charges;
      const matches = Math.abs(total - expected) <= toleranceCents;
      // Los descuentos globales que no afectan la base reducen el importe a pagar;
      // los que sí la afectan ya vienen restados. Se aceptan ambas lecturas.
      const matchesWithDiscount =
        discount !== null && Math.abs(total - (expected - discount)) <= toleranceCents;
      if (!matches && !matchesWithDiscount) {
        issues.push(
          issue(
            'TOTAL_MISMATCH',
            SEVERITY.ERROR,
            'El total no coincide con la base más los impuestos y cargos',
            {
              expected: formatCents(expected),
              actual: formatCents(total),
            },
          ),
        );
      }
    }
  }

  if (hasBreakdown && subtotal !== null) {
    const breakdown = sumCents(taxable, exempt, unaffected);
    const ok =
      Math.abs(subtotal - breakdown) <= toleranceCents ||
      (discount !== null && Math.abs(subtotal - discount - breakdown) <= toleranceCents);
    if (!ok) {
      issues.push(
        issue(
          'SUBTOTAL_MISMATCH',
          SEVERITY.WARNING,
          'El subtotal no coincide con la suma de operaciones gravadas, exoneradas e inafectas',
          { expected: formatCents(breakdown), actual: formatCents(subtotal) },
        ),
      );
    }
  }

  // El IGV solo se aplica a la base gravada. Sin desglose, usar el subtotal es
  // una suposición (puede haber operaciones exoneradas), por eso solo se advierte.
  const igvBase = taxable ?? (hasBreakdown ? null : subtotal);
  if (igv !== null && igvBase !== null) {
    if (igvBase === 0 && igv > 0) {
      issues.push(
        issue(
          'IGV_WITHOUT_TAXABLE_BASE',
          SEVERITY.WARNING,
          'Hay IGV pero la base gravada es cero',
          { actual: formatCents(igv) },
        ),
      );
    } else if (igvBase > 0) {
      const fits = igvRatesBp.some(
        (bp) => Math.abs(applyRateBasisPoints(igvBase, bp) - igv) <= toleranceCents,
      );
      if (!fits) {
        issues.push(
          issue(
            'IGV_RATE_MISMATCH',
            SEVERITY.WARNING,
            `El IGV no corresponde a ninguna tasa configurada (${igvRatesBp.map((bp) => `${bp / 100} %`).join(', ')}) sobre la base gravada`,
            {
              expected: formatCents(applyRateBasisPoints(igvBase, igvRatesBp[0])),
              actual: formatCents(igv),
            },
          ),
        );
      }
    }
  } else if (igv === null && taxable !== null && taxable > 0) {
    issues.push(
      issue('IGV_MISSING', SEVERITY.WARNING, 'Hay base gravada pero no se identificó el IGV'),
    );
  }

  return issues;
}

/**
 * Valida una factura normalizada. Es una función pura: no consulta la base de
 * datos ni la fecha del sistema (se recibe `today` para que sea reproducible).
 *
 * @returns {{ status: 'valid' | 'warning' | 'error', issues: Array<object> }}
 */
export function validateInvoice(
  invoice,
  {
    normalizationIssues = [],
    toleranceCents = 2,
    today = new Date().toISOString().slice(0, 10),
    igvRatesBp = DEFAULT_IGV_RATES_BP,
  } = {},
) {
  const issues = [
    ...checkRequired(invoice),
    ...checkRucs(invoice),
    ...checkDates(invoice, today),
    ...checkIdentification(invoice),
    ...checkAmounts(invoice, normalizationIssues, toleranceCents, igvRatesBp),
  ];
  return { status: statusFromIssues(issues), issues };
}

export function statusFromIssues(issues) {
  if (issues.some((i) => i.severity === SEVERITY.ERROR)) return VALIDATION_STATUS.ERROR;
  if (issues.some((i) => i.severity === SEVERITY.WARNING)) return VALIDATION_STATUS.WARNING;
  return VALIDATION_STATUS.VALID;
}
