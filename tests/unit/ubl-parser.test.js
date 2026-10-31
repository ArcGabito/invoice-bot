import { describe, expect, it } from 'vitest';
import { parseUbl } from '../../src/infrastructure/documents/ubl-parser.js';
import { normalizeInvoice } from '../../src/domain/invoice.js';
import { validateInvoice } from '../../src/domain/invoice-validation.js';
import { DocumentReadError } from '../../src/shared/errors.js';
import { buildCdr, buildUblInvoice } from '../helpers/ubl-builder.js';

const parse = (xml, encoding = 'utf8') => parseUbl(Buffer.from(xml, encoding));

function validateParsed(data) {
  const { invoice, normalizationIssues } = normalizeInvoice(data);
  return validateInvoice(invoice, { normalizationIssues, today: '2026-10-30', toleranceCents: 2 });
}

describe('parseUbl', () => {
  it('lee una factura electrónica SUNAT sin perder ceros ni decimales', () => {
    const result = parse(buildUblInvoice());
    expect(result.kind).toBe('invoice');
    expect(result.data).toMatchObject({
      is_invoice: true,
      supplier_name: 'PROVEEDOR DE EJEMPLO S.A.C.',
      supplier_ruc: '20100047218',
      customer_name: 'CLIENTE DE PRUEBA E.I.R.L.',
      customer_ruc: '20131312955',
      document_type: '01',
      series: 'F001',
      number: '00001234',
      issue_date: '2026-10-15',
      currency: 'PEN',
      subtotal: '100.00',
      taxable_amount: '100.00',
      igv: '18.00',
      total: '118.00',
      exempt_amount: null,
      notes: [],
    });
    expect(validateParsed(result.data).status).toBe('valid');
  });

  it('reparte gravadas, exoneradas, inafectas, ISC e ICBPER según el catálogo de tributos', () => {
    const { data } = parse(
      buildUblInvoice({
        taxes: [
          { scheme: '1000', taxable: '100.00', tax: '18.00' },
          { scheme: '9997', taxable: '30.00', tax: '0.00' },
          { scheme: '9998', taxable: '20.00', tax: '0.00' },
          { scheme: '7152', taxable: '1.00', tax: '0.50' },
        ],
        lineExtension: '150.00',
        payable: '168.50',
      }),
    );
    expect(data).toMatchObject({
      taxable_amount: '100.00',
      exempt_amount: '30.00',
      unaffected_amount: '20.00',
      icbper: '0.50',
      total: '168.50',
    });
    expect(validateParsed(data).status).toBe('valid');
  });

  it('suma exportaciones a inafectas y lo deja anotado', () => {
    const { data } = parse(
      buildUblInvoice({
        currency: 'USD',
        taxes: [
          { scheme: '9998', taxable: '50.00', tax: '0.00' },
          { scheme: '9995', taxable: '1000.00', tax: '0.00' },
        ],
        lineExtension: '1050.00',
        payable: '1050.00',
      }),
    );
    expect(data.unaffected_amount).toBe('1050.00');
    expect(data.currency).toBe('USD');
    expect(data.notes[0]).toMatch(/exportación/);
    expect(validateParsed(data).status).toBe('valid');
  });

  it('lee descuentos globales y otros cargos', () => {
    const { data } = parse(
      buildUblInvoice({ allowance: '8.00', charge: '2.00', payable: '112.00' }),
    );
    expect(data).toMatchObject({ discount_amount: '8.00', other_charges: '2.00', total: '112.00' });
    expect(validateParsed(data).status).toBe('valid');
  });

  it('identifica notas de crédito y de débito por su elemento raíz', () => {
    expect(parse(buildUblInvoice({ root: 'CreditNote', id: 'FC01-00000007' })).data).toMatchObject({
      document_type: '07',
      series: 'FC01',
      number: '00000007',
    });
    const debit = parse(buildUblInvoice({ root: 'DebitNote', id: 'FD01-00000003' })).data;
    expect(debit).toMatchObject({ document_type: '08', total: '118.00' });
  });

  it('lee boletas con DNI del cliente', () => {
    const { data } = parse(
      buildUblInvoice({
        id: 'B001-00000045',
        typeCode: '03',
        customerId: '46779354',
        customerScheme: '1',
      }),
    );
    expect(data).toMatchObject({ document_type: '03', series: 'B001', customer_ruc: '46779354' });
    expect(validateParsed(data).status).toBe('valid');
  });

  it('respeta la codificación ISO-8859-1 declarada', () => {
    const xml = buildUblInvoice({
      encoding: 'ISO-8859-1',
      supplierName: 'PANIFICADORA PEÑA S.A.C.',
    });
    expect(parse(xml, 'latin1').data.supplier_name).toBe('PANIFICADORA PEÑA S.A.C.');
  });

  it('reconoce la constancia de recepción (CDR) de SUNAT', () => {
    expect(parse(buildCdr())).toEqual({ kind: 'cdr' });
  });

  it('distingue un XML que no es un comprobante', () => {
    expect(parse('<?xml version="1.0"?><config><a>1</a></config>')).toEqual({
      kind: 'other',
      root: 'config',
    });
  });

  it('rechaza un XML mal formado', () => {
    expect(() => parse('<Invoice><cbc:ID>F001</Invoice')).toThrow(DocumentReadError);
  });

  it('no resuelve entidades externas (XXE)', () => {
    const xml = `<?xml version="1.0"?>
<!DOCTYPE Invoice [<!ENTITY xxe SYSTEM "file:///C:/Windows/win.ini">]>
<Invoice xmlns:cbc="urn:cbc"><cbc:ID>&xxe;</cbc:ID></Invoice>`;
    let result;
    try {
      result = parse(xml);
    } catch (error) {
      expect(error).toBeInstanceOf(DocumentReadError);
      return;
    }
    expect(JSON.stringify(result)).not.toMatch(/fonts|extensions|\[/i);
  });
});

describe('parseUbl con anticipos', () => {
  it('lee PrepaidAmount y la factura queda válida', () => {
    const { data } = parse(buildUblInvoice({ prepaid: '50.00', payable: '68.00' }));
    expect(data).toMatchObject({ prepaid_amount: '50.00', total: '68.00', notes: [] });
    expect(validateParsed(data).status).toBe('valid');
  });
});
