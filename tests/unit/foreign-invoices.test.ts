import { describe, expect, it } from 'vitest';
import { normalizeDate, normalizeInvoice } from '../../src/domain/invoice.ts';
import { validateInvoice } from '../../src/domain/invoice-validation.ts';
import { businessKey } from '../../src/domain/duplicate-detection.ts';
import { joinTextItems } from '../../src/infrastructure/documents/pdf-extractor.ts';

/** Fragmento de texto como lo entrega pdf.js: posición x, ancho y tamaño de letra 10. */
const item = (str, x, width, { y = 700, eol = false } = {}) => ({
  str,
  transform: [10, 0, 0, 10, x, y],
  width,
  height: 10,
  hasEOL: eol,
});

describe('joinTextItems', () => {
  it('recupera el guion sin texto Unicode entre letras o dígitos', async () => {
    const items = [
      item('Invoice number', 30, 60),
      item(' ', 90, 8),
      item('IN', 103.5, 9.1),
      item('\u0000', 112.6, 4.2),
      item('45946823', 116.8, 46.6, { eol: true }),
    ];
    expect(joinTextItems(items)).toBe('Invoice number IN-45946823');
  });

  it('descarta el glifo sin texto cuando no está entre letras o dígitos (paréntesis)', () => {
    const items = [
      item('PE VAT - PERU ', 30, 70),
      item('\u0000', 100, 3),
      item('0% on $0.00', 103, 50),
      item('\u0000', 153, 3),
      item(' $0.00', 160, 25),
    ];
    expect(joinTextItems(items)).toBe('PE VAT - PERU 0% on $0.00 $0.00');
  });

  it('une fragmentos pegados y separa los que tienen un hueco', async () => {
    const items = [
      item('Sub', 30, 15),
      item('total', 45, 20),
      item('$10.44', 120, 25),
      item('Total', 30, 20, { y: 680 }),
      item('$10.44', 120, 25, { y: 680 }),
    ];
    expect(joinTextItems(items)).toBe('Subtotal $10.44\nTotal $10.44');
  });
});

describe('normalizeDate', () => {
  it.each([
    ['August 5, 2025', '2025-08-05'],
    ['Oct 19, 2026', '2026-10-19'],
    ['Sept. 4 2026', '2026-09-04'],
    ['5 de agosto de 2025', '2025-08-05'],
    ['15 setiembre 2026', '2026-09-15'],
    ['15/10/2026', '2026-10-15'],
  ])('%s → %s', (input, expected) => {
    expect(normalizeDate(input)).toBe(expected);
  });

  it('deja tal cual lo que no entiende, para que la validación lo marque', async () => {
    expect(normalizeDate('Brumario 18, 2026')).toBe('Brumario 18, 2026');
  });
});

const foreign = (overrides = {}) =>
  normalizeInvoice({
    supplier_name: 'Proveedor Digital, Inc.',
    supplier_ruc: null,
    supplier_tax_id: '12-3456789',
    supplier_country: 'us',
    document_type: '91',
    series: null,
    number: 'IN-12345678',
    issue_date: 'August 5, 2026',
    currency: 'USD',
    subtotal: '10.44',
    igv: null,
    other_taxes: '0.00',
    total: '10.44',
    ...overrides,
  });

function validate(overrides = {}) {
  const { invoice, normalizationIssues } = foreign(overrides);
  return validateInvoice(invoice, { normalizationIssues, today: '2026-10-31', toleranceCents: 2 });
}
const codes = (r) => r.issues.map((i) => i.rule_code);

describe('comprobantes del exterior (tipo 91)', () => {
  it('normaliza país, fecha y conserva el identificador fiscal', async () => {
    expect(foreign().invoice).toMatchObject({
      supplier_tax_id: '12-3456789',
      supplier_country: 'US',
      issue_date: '2026-08-05',
      document_type: '91',
    });
  });

  it.each([
    ['United States', 'US'],
    ['EE. UU.', 'US'],
    ['Ireland', 'IE'],
    ['Perú', 'PE'],
    ['Narnia', 'Narnia'],
  ])('convierte el país "%s" a "%s"', (country, expected) => {
    expect(foreign({ supplier_country: country }).invoice.supplier_country).toBe(expected);
  });

  it('es válido sin RUC ni serie, con una nota informativa sobre sus obligaciones', async () => {
    const result = validate();
    expect(result.status).toBe('valid');
    expect(result.issues).toEqual([
      expect.objectContaining({
        rule_code: 'FOREIGN_SUPPLIER',
        severity: 'info',
        actual_value: 'Proveedor Digital, Inc. · US',
      }),
    ]);
  });

  it('exige identificar al proveedor por nombre o identificador fiscal', async () => {
    const result = validate({ supplier_name: null, supplier_tax_id: null });
    expect(result.status).toBe('error');
    expect(result.issues[0].message).toMatch(/proveedor del exterior/);
    expect(validate({ supplier_tax_id: null }).status).toBe('valid');
  });

  it('sigue verificando el total con el impuesto extranjero en otros tributos', async () => {
    expect(validate({ other_taxes: '1.88', total: '12.32' }).status).toBe('valid');
    expect(codes(validate({ total: '15.00' }))).toContain('TOTAL_MISMATCH');
  });

  it('advierte si trae IGV o un RUC peruano (probablemente mal clasificado)', () => {
    expect(codes(validate({ igv: '1.88', total: '12.32' }))).toContain('IGV_ON_FOREIGN_INVOICE');
    expect(codes(validate({ supplier_ruc: '20100047218' }))).toContain('FOREIGN_WITH_RUC');
  });

  it('no aplica las reglas de serie, correlativo SUNAT ni monedas habituales', async () => {
    const result = validate({ currency: 'EUR', number: 'INV/2026/000123' });
    expect(codes(result)).not.toContain('SERIES_FORMAT');
    expect(codes(result)).not.toContain('NUMBER_FORMAT');
    expect(codes(result)).not.toContain('CURRENCY_UNUSUAL');
  });

  it('identifica duplicados por identificador fiscal o, si falta, por nombre normalizado', async () => {
    const key = (o = {}) => businessKey(foreign(o).invoice);
    expect(key()).toBe('ID:123456789|91|IN-12345678');
    expect(key({ supplier_tax_id: null, supplier_name: 'PROVEEDOR DIGITAL INC' })).toBe(
      key({ supplier_tax_id: null, supplier_name: 'Proveedor Digital, Inc.' }),
    );
    expect(key({ number: null })).toBeNull();
  });
});
