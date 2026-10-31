import { describe, expect, it } from 'vitest';
import { normalizeInvoice } from '../../src/domain/invoice.js';
import { validateInvoice } from '../../src/domain/invoice-validation.js';
import { businessKey } from '../../src/domain/duplicate-detection.js';

const TODAY = '2026-10-28';

function validate(overrides = {}, options = {}) {
  const { invoice, normalizationIssues } = normalizeInvoice({
    supplier_name: 'Proveedor de ejemplo S.A.C.',
    supplier_ruc: '20100047218',
    document_type: '01',
    series: 'F001',
    number: '00001234',
    issue_date: '2026-10-15',
    currency: 'PEN',
    subtotal: '100.00',
    igv: '18.00',
    total: '118.00',
    ...overrides,
  });
  return validateInvoice(invoice, {
    normalizationIssues,
    today: TODAY,
    toleranceCents: 2,
    ...options,
  });
}

const codes = (result) => result.issues.map((i) => i.rule_code);

describe('validateInvoice', () => {
  it('marca como válida una factura coherente', () => {
    const result = validate();
    expect(result.issues).toEqual([]);
    expect(result.status).toBe('valid');
  });

  it('exige los campos obligatorios sin inventarlos', () => {
    const result = validate({ supplier_ruc: null, issue_date: null, total: null });
    expect(result.status).toBe('error');
    expect(result.issues.filter((i) => i.rule_code === 'REQUIRED_FIELD_MISSING')).toHaveLength(3);
  });

  it('detecta un RUC con dígito de control incorrecto', () => {
    const result = validate({ supplier_ruc: '20100047219' });
    expect(result.status).toBe('error');
    expect(result.issues).toContainEqual(
      expect.objectContaining({
        rule_code: 'SUPPLIER_RUC_CHECK_DIGIT',
        expected_value: '8',
        actual_value: '9',
      }),
    );
  });

  it('una fecha inválida impide marcar la factura como válida', () => {
    expect(validate({ issue_date: '2026-02-30' }).status).toBe('error');
    expect(codes(validate({ issue_date: '2026-02-30' }))).toContain('ISSUE_DATE_INVALID');
  });

  it('acepta fechas en formato peruano DD/MM/AAAA', () => {
    expect(validate({ issue_date: '15/10/2026' }).status).toBe('valid');
  });

  it('advierte sobre fechas futuras', () => {
    const result = validate({ issue_date: '2026-11-15' });
    expect(result.status).toBe('warning');
    expect(codes(result)).toContain('ISSUE_DATE_FUTURE');
  });

  it('marca error cuando el total no cuadra más allá de la tolerancia', () => {
    const result = validate({ total: '120.00' });
    expect(result.status).toBe('error');
    expect(result.issues).toContainEqual(
      expect.objectContaining({
        rule_code: 'TOTAL_MISMATCH',
        expected_value: '118.00',
        actual_value: '120.00',
      }),
    );
  });

  it('tolera diferencias de redondeo dentro de la tolerancia', () => {
    expect(validate({ total: '118.02' }).status).toBe('valid');
    expect(validate({ total: '118.03' }).status).toBe('error');
  });

  it('usa el desglose tributario: exoneradas e inafectas no llevan IGV', () => {
    const result = validate({
      subtotal: '150.00',
      taxable_amount: '100.00',
      exempt_amount: '30.00',
      unaffected_amount: '20.00',
      igv: '18.00',
      total: '168.00',
    });
    expect(result.issues).toEqual([]);
  });

  it('acepta una factura solo exonerada sin IGV', () => {
    const result = validate({
      subtotal: null,
      exempt_amount: '250.00',
      igv: null,
      total: '250.00',
    });
    expect(result.status).toBe('valid');
  });

  it('acepta descuentos globales que reducen el total', () => {
    const result = validate({
      taxable_amount: '100.00',
      subtotal: null,
      igv: '18.00',
      discount_amount: '8.00',
      total: '110.00',
    });
    expect(result.status).toBe('valid');
  });

  it('acepta el IGV de 10,5 % del régimen de restaurantes', () => {
    const result = validate({
      taxable_amount: '100.00',
      subtotal: null,
      igv: '10.50',
      total: '110.50',
    });
    expect(result.status).toBe('valid');
  });

  it('advierte cuando el IGV no corresponde a ninguna tasa', () => {
    const result = validate({ igv: '15.00', total: '115.00' });
    expect(result.status).toBe('warning');
    expect(result.issues).toContainEqual(
      expect.objectContaining({ rule_code: 'IGV_RATE_MISMATCH', expected_value: '18.00' }),
    );
  });

  it('incluye ICBPER en la aritmética del total', () => {
    const result = validate({ icbper: '0.50', total: '118.50' });
    expect(result.status).toBe('valid');
  });

  it('marca error en importes que no se pueden interpretar', () => {
    const result = validate({ igv: 'dieciocho' });
    expect(codes(result)).toContain('AMOUNT_UNPARSEABLE');
    expect(result.status).toBe('error');
  });

  it('advierte sobre series que no corresponden al tipo', () => {
    expect(codes(validate({ series: 'B001' }))).toContain('SERIES_TYPE_MISMATCH');
    expect(codes(validate({ series: 'XYZ' }))).toContain('SERIES_FORMAT');
  });

  it('rechaza monedas que no son ISO 4217', () => {
    expect(codes(validate({ currency: 'Soles peruanos' }))).toContain('CURRENCY_INVALID');
    expect(validate({ currency: 'S/' }).status).toBe('valid');
  });

  it('una factura con advertencias no se presenta como válida', () => {
    expect(validate({ supplier_name: null }).status).toBe('warning');
  });
});

describe('businessKey', () => {
  it('ignora ceros a la izquierda del número y mayúsculas de la serie', () => {
    const a = businessKey({
      supplier_ruc: '20100047218',
      document_type: '01',
      series: 'f001',
      number: '00001234',
    });
    const b = businessKey({
      supplier_ruc: '20100047218',
      document_type: '01',
      series: 'F001',
      number: '1234',
    });
    expect(a).toBe(b);
  });

  it('devuelve null si falta alguna parte de la clave', () => {
    expect(
      businessKey({
        supplier_ruc: '20100047218',
        document_type: '01',
        series: 'F001',
        number: null,
      }),
    ).toBeNull();
  });
});

describe('anticipos', () => {
  it('acepta un total que ya descuenta los anticipos', () => {
    // Base 100 + IGV 18 = 118; anticipo 50 ya pagado → a pagar 68.
    const result = validate({ prepaid_amount: '50.00', total: '68.00' });
    expect(result.issues).toEqual([]);
  });

  it('acepta también el anticipo restado de la base (según el emisor)', () => {
    expect(validate({ prepaid_amount: '50.00', total: '118.00' }).status).toBe('valid');
  });

  it('un total cero por anticipo completo no es una operación gratuita', () => {
    const result = validate({ prepaid_amount: '118.00', total: '0.00' });
    expect(result.issues).toEqual([]);
  });

  it('sigue detectando totales incoherentes con anticipos', () => {
    const result = validate({ prepaid_amount: '50.00', total: '60.00' });
    expect(result.issues).toContainEqual(
      expect.objectContaining({ rule_code: 'TOTAL_MISMATCH', expected_value: '68.00' }),
    );
    expect(result.issues[0].message).toMatch(/anticipos/);
  });
});
