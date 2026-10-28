import { ExtractionSchema, EXTRACTION_FIELDS } from '../extraction-schema.js';

/**
 * Proveedor sin IA basado en expresiones regulares. Sirve para pruebas
 * automatizadas y para probar el flujo completo sin coste ni conexión.
 * Solo reconoce comprobantes con etiquetas convencionales; no sustituye a un LLM.
 */
export class MockProvider {
  name = 'mock';
  model = 'regex-heuristics-v1';

  async extract({ text }) {
    const data = Object.fromEntries(EXTRACTION_FIELDS.map((f) => [f, null]));
    data.notes = [];

    const lines = text
      .split('\n')
      .map((l) => l.trim())
      .filter(Boolean);
    const find = (re) => {
      const m = re.exec(text);
      return m ? m[1].trim() : null;
    };
    const amountAfter = (label) => find(new RegExp(`${label}[^\\d\\n]*([\\d.,]+)`, 'i'));

    data.is_invoice = /factura|boleta|nota de (cr[ée]dito|d[ée]bito)|comprobante/i.test(text);

    const rucs = [...text.matchAll(/R\.?U\.?C\.?\s*(?:N[°º.o]*)?\s*:?\s*(\d{11})/gi)].map(
      (m) => m[1],
    );
    data.supplier_ruc = rucs[0] ?? null;
    data.customer_ruc = rucs[1] ?? find(/DNI\s*:?\s*(\d{8})/i);

    if (/nota de cr[ée]dito/i.test(text)) data.document_type = '07';
    else if (/nota de d[ée]bito/i.test(text)) data.document_type = '08';
    else if (/boleta/i.test(text)) data.document_type = '03';
    else if (/factura/i.test(text)) data.document_type = '01';

    const seriesMatch = /\b([FBE][A-Z0-9]{3}|\d{4})\s*-\s*(\d{1,8})\b/.exec(text);
    if (seriesMatch) {
      data.series = seriesMatch[1];
      data.number = seriesMatch[2];
    }

    const date = find(/fecha\s+de\s+emisi[oó]n\s*:?\s*([\d/.-]{8,10})/i);
    if (date) {
      const dmy = /^(\d{2})[/.-](\d{2})[/.-](\d{4})$/.exec(date);
      data.issue_date = dmy ? `${dmy[3]}-${dmy[2]}-${dmy[1]}` : date;
    }

    if (/US\$|d[oó]lares|\bUSD\b/i.test(text)) data.currency = 'USD';
    else if (/S\/|soles|\bPEN\b/i.test(text)) data.currency = 'PEN';

    const nameLine = lines.find((l) => !/ruc|factura|boleta|nota|fecha|electr[oó]nica/i.test(l));
    data.supplier_name = nameLine ?? null;
    data.customer_name = find(/(?:se[ñn]or(?:\(es\))?|cliente|raz[oó]n social)\s*:\s*([^\n]+)/i);

    data.taxable_amount = amountAfter('op(?:\\.|eraci[oó]n(?:es)?)?\\s*gravadas?');
    data.exempt_amount = amountAfter('op(?:\\.|eraci[oó]n(?:es)?)?\\s*exoneradas?');
    data.unaffected_amount = amountAfter('op(?:\\.|eraci[oó]n(?:es)?)?\\s*inafectas?');
    data.subtotal = amountAfter('sub\\s*total');
    data.discount_amount = amountAfter('descuentos?');
    data.igv = amountAfter('\\bI\\.?G\\.?V\\.?(?:\\s*\\d+\\s*%)?');
    data.icbper = amountAfter('ICBPER');
    data.total = amountAfter(
      '(?:importe\\s+)?total(?!\\s*(?:de\\s+)?descuentos?)(?:\\s+a\\s+pagar)?',
    );

    for (const field of [
      'taxable_amount',
      'exempt_amount',
      'unaffected_amount',
      'subtotal',
      'discount_amount',
      'igv',
      'icbper',
      'total',
    ]) {
      if (data[field]) data[field] = data[field].replace(/,(?=\d{3}\b)/g, '');
    }

    const parsed = ExtractionSchema.parse(data);
    return {
      data: parsed,
      raw: JSON.stringify(parsed),
      model: this.model,
      usage: { input_tokens: null, output_tokens: null },
    };
  }

  async ping() {
    return 'proveedor simulado (sin IA)';
  }
}
