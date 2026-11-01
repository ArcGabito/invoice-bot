import { describe, expect, it } from 'vitest';
import { normalizeInvoice } from '../../src/domain/invoice.ts';
import { validateInvoice } from '../../src/domain/invoice-validation.ts';
import {
  ANNEX3_THRESHOLD_CENTS,
  DETRACTION_CODES,
  checkDetraction,
  netPayableCents,
  totalInPen,
} from '../../src/domain/detraction.ts';
import { parseUbl } from '../../src/infrastructure/documents/ubl-parser.ts';
import { buildUblInvoice } from '../helpers/ubl-builder.ts';

/** Servicio de S/ 1,000 + IGV sujeto a detracción del 12 % (código 037). */
function invoice(overrides = {}) {
  return normalizeInvoice({
    supplier_name: 'Proveedor de ejemplo S.A.C.',
    supplier_ruc: '20100047218',
    document_type: '01',
    series: 'F001',
    number: '00001234',
    issue_date: '2026-10-15',
    currency: 'PEN',
    subtotal: '1000.00',
    igv: '180.00',
    total: '1180.00',
    detraction_code: '037',
    detraction_percent: '12',
    detraction_amount: '141.60',
    detraction_account: '00-000-123456',
    ...overrides,
  });
}

const codes = (issues: { rule_code: string }[]) => issues.map((i) => i.rule_code);

describe('detracciones (SPOT)', () => {
  it('tiene las tasas vigentes de SUNAT para los códigos más comunes', () => {
    expect(DETRACTION_CODES['037']).toMatchObject({ rateBp: 1200, annex: 3 });
    expect(DETRACTION_CODES['030']).toMatchObject({ rateBp: 400, annex: 3 });
    expect(DETRACTION_CODES['022']).toMatchObject({ rateBp: 1200, annex: 3 });
    expect(DETRACTION_CODES['027']).toBeUndefined(); // transporte de carga: régimen propio
    expect(ANNEX3_THRESHOLD_CENTS).toBe(70_000);
  });

  it('normaliza código y porcentaje como vienen en los comprobantes', () => {
    const { invoice: i } = invoice({ detraction_code: '37', detraction_percent: '12,00 %' });
    expect(i).toMatchObject({
      detraction_code: '037',
      detraction_percent: 12,
      detraction_amount_cents: 14_160,
      detraction_account: '00-000-123456',
    });
    const bad = invoice({ detraction_percent: 'doce' });
    expect(bad.invoice.detraction_percent).toBeNull();
    expect(bad.normalizationIssues.map((n) => n.field)).toContain('detraction_percent');
  });

  it('no dice nada de un comprobante sin detracción', () => {
    const { invoice: i } = invoice({
      detraction_code: null,
      detraction_percent: null,
      detraction_amount: null,
      detraction_account: null,
    });
    expect(checkDetraction(i)).toEqual([]);
    expect(netPayableCents(i, null)).toBe(118_000);
  });

  it('acepta una detracción correcta y calcula el neto a pagar', () => {
    const { invoice: i, normalizationIssues } = invoice();
    expect(checkDetraction(i)).toEqual([]);
    expect(netPayableCents(i, null)).toBe(118_000 - 14_160);
    const result = validateInvoice(i, { normalizationIssues, today: '2026-10-28' });
    expect(result.status).toBe('valid');
  });

  it('acepta el monto redondeado a soles enteros', () => {
    expect(checkDetraction(invoice({ detraction_amount: '142.00' }).invoice)).toEqual([]);
    expect(codes(checkDetraction(invoice({ detraction_amount: '150.00' }).invoice))).toEqual([
      'DETRACTION_AMOUNT_MISMATCH',
    ]);
  });

  it('advierte si el porcentaje no es el de SUNAT para el código', () => {
    const issues = checkDetraction(
      invoice({ detraction_percent: '10', detraction_amount: '118.00' }).invoice,
    );
    expect(issues).toEqual([
      expect.objectContaining({
        rule_code: 'DETRACTION_RATE_MISMATCH',
        severity: 'warning',
        expected_value: '12 %',
        actual_value: '10 %',
      }),
    ]);
  });

  it('advierte si falta parte de los datos y avisa de códigos desconocidos', () => {
    expect(
      codes(
        checkDetraction(invoice({ detraction_amount: null, detraction_percent: null }).invoice),
      ),
    ).toEqual(['DETRACTION_INCOMPLETE']);
    const unknown = checkDetraction(invoice({ detraction_code: '099' }).invoice);
    expect(unknown).toEqual([
      expect.objectContaining({ rule_code: 'DETRACTION_UNKNOWN_CODE', severity: 'info' }),
    ]);
  });

  it('avisa si un servicio del Anexo 3 no supera S/ 700', () => {
    const issues = checkDetraction(
      invoice({ subtotal: '500.00', igv: '90.00', total: '590.00', detraction_amount: '70.80' })
        .invoice,
    );
    expect(codes(issues)).toEqual(['DETRACTION_BELOW_THRESHOLD']);
  });

  it('en dólares compara el monto en soles con el tipo de cambio', () => {
    const { invoice: usd } = invoice({ currency: 'USD', detraction_amount: '531.00' });
    // 1,180.00 USD × 3.750 = S/ 4,425.00; 12 % = S/ 531.00
    expect(totalInPen(usd, 3.75)).toBe(442_500);
    expect(checkDetraction(usd, { exchangeRate: 3.75 })).toEqual([]);
    expect(netPayableCents(usd, 3.75)).toBe(118_000 - 14_160);
    // Sin tipo de cambio no se puede comprobar el monto ni el neto.
    expect(checkDetraction(usd)).toEqual([]);
    expect(netPayableCents(usd, null)).toBeNull();
    // Monto calculado sobre el total en dólares (error frecuente).
    const wrong = invoice({ currency: 'USD', detraction_amount: '141.60' }).invoice;
    expect(codes(checkDetraction(wrong, { exchangeRate: 3.75 }))).toEqual([
      'DETRACTION_AMOUNT_MISMATCH',
    ]);
  });
});

describe('detracción en el XML de SUNAT', () => {
  it('lee código, porcentaje, monto y cuenta del Banco de la Nación', () => {
    const result = parseUbl(
      Buffer.from(
        buildUblInvoice({
          taxes: [{ scheme: '1000', taxable: '1000.00', tax: '180.00' }],
          lineExtension: '1000.00',
          payable: '1180.00',
          dueDate: '2026-11-15',
          detraction: { code: '037', percent: '12.00', amount: '141.60' },
        }),
      ),
    );
    if (result.kind !== 'invoice') throw new Error(result.kind);
    expect(result.data).toMatchObject({
      detraction_code: '037',
      detraction_percent: '12.00',
      detraction_amount: '141.60',
      detraction_account: '00-000-123456',
      due_date: '2026-11-15',
    });
    const { invoice: i } = normalizeInvoice(result.data);
    expect(checkDetraction(i)).toEqual([]);
  });

  it('deja los campos vacíos si el comprobante no está sujeto', () => {
    const result = parseUbl(Buffer.from(buildUblInvoice()));
    if (result.kind !== 'invoice') throw new Error(result.kind);
    expect(result.data).toMatchObject({
      detraction_code: null,
      detraction_percent: null,
      detraction_amount: null,
      detraction_account: null,
    });
  });
});
