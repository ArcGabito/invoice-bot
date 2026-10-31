import fs from 'node:fs';

const NAMESPACES: Record<string, string> = {
  Invoice: 'urn:oasis:names:specification:ubl:schema:xsd:Invoice-2',
  CreditNote: 'urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2',
  DebitNote: 'urn:oasis:names:specification:ubl:schema:xsd:DebitNote-2',
};

const TAX_NAMES: Record<string, [string, string]> = {
  1000: ['IGV', 'VAT'],
  2000: ['ISC', 'EXC'],
  7152: ['ICBPER', 'OTH'],
  9995: ['EXP', 'FRE'],
  9996: ['GRA', 'FRE'],
  9997: ['EXO', 'VAT'],
  9998: ['INA', 'FRE'],
  9999: ['OTROS', 'OTH'],
};

/**
 * Genera un comprobante electrónico ficticio en UBL 2.1 con la estructura de
 * SUNAT (namespaces, prefijos cbc/cac, firma vacía). Los importes se pasan
 * como texto, igual que en un XML real.
 */
export function buildUblInvoice({
  root = 'Invoice',
  id = 'F001-00001234',
  typeCode = '01',
  issueDate = '2026-10-15',
  dueDate = null,
  currency = 'PEN',
  supplierRuc = '20100047218',
  supplierName = 'PROVEEDOR DE EJEMPLO S.A.C.',
  customerId = '20131312955',
  customerScheme = '6',
  customerName = 'CLIENTE DE PRUEBA E.I.R.L.',
  taxes = [{ scheme: '1000', taxable: '100.00', tax: '18.00' }],
  lineExtension = '100.00',
  allowance = null,
  charge = null,
  prepaid = null,
  payable = '118.00',
  encoding = 'UTF-8',
}: {
  root?: string;
  id?: string;
  typeCode?: string;
  issueDate?: string;
  dueDate?: string | null;
  currency?: string;
  supplierRuc?: string;
  supplierName?: string;
  customerId?: string;
  customerScheme?: string;
  customerName?: string;
  taxes?: { scheme: string; taxable: string; tax: string }[];
  lineExtension?: string | null;
  allowance?: string | null;
  charge?: string | null;
  prepaid?: string | null;
  payable?: string | null;
  encoding?: string;
} = {}): string {
  const amount = (tag: string, value: string | null) =>
    value === null ? '' : `<cbc:${tag} currencyID="${currency}">${value}</cbc:${tag}>`;
  const taxTotal = taxes.reduce((acc, t) => acc + Math.round(Number(t.tax) * 100), 0) / 100;
  const monetary = root === 'DebitNote' ? 'RequestedMonetaryTotal' : 'LegalMonetaryTotal';
  const typeElement =
    root === 'Invoice'
      ? `<cbc:InvoiceTypeCode listID="0101">${typeCode}</cbc:InvoiceTypeCode>`
      : '';

  return `<?xml version="1.0" encoding="${encoding}"?>
<${root} xmlns="${NAMESPACES[root]}"
  xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
  xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"
  xmlns:ds="http://www.w3.org/2000/09/xmldsig#"
  xmlns:ext="urn:oasis:names:specification:ubl:schema:xsd:CommonExtensionComponents-2">
  <ext:UBLExtensions><ext:UBLExtension><ext:ExtensionContent><ds:Signature Id="SignSUNAT"/></ext:ExtensionContent></ext:UBLExtension></ext:UBLExtensions>
  <cbc:UBLVersionID>2.1</cbc:UBLVersionID>
  <cbc:CustomizationID>2.0</cbc:CustomizationID>
  <cbc:ID>${id}</cbc:ID>
  <cbc:IssueDate>${issueDate}</cbc:IssueDate>
  ${dueDate ? `<cbc:DueDate>${dueDate}</cbc:DueDate>` : ''}
  ${typeElement}
  <cbc:Note languageLocaleID="1000">CIENTO DIECIOCHO CON 00/100 SOLES</cbc:Note>
  <cbc:DocumentCurrencyCode>${currency}</cbc:DocumentCurrencyCode>
  <cac:AccountingSupplierParty><cac:Party>
    <cac:PartyIdentification><cbc:ID schemeID="6">${supplierRuc}</cbc:ID></cac:PartyIdentification>
    <cac:PartyName><cbc:Name>NOMBRE COMERCIAL</cbc:Name></cac:PartyName>
    <cac:PartyLegalEntity><cbc:RegistrationName><![CDATA[${supplierName}]]></cbc:RegistrationName></cac:PartyLegalEntity>
  </cac:Party></cac:AccountingSupplierParty>
  <cac:AccountingCustomerParty><cac:Party>
    <cac:PartyIdentification><cbc:ID schemeID="${customerScheme}">${customerId}</cbc:ID></cac:PartyIdentification>
    <cac:PartyLegalEntity><cbc:RegistrationName>${customerName}</cbc:RegistrationName></cac:PartyLegalEntity>
  </cac:Party></cac:AccountingCustomerParty>
  <cac:TaxTotal>
    ${amount('TaxAmount', taxTotal.toFixed(2))}
    ${taxes
      .map(
        (t) => `<cac:TaxSubtotal>
      ${amount('TaxableAmount', t.taxable)}
      ${amount('TaxAmount', t.tax)}
      <cac:TaxCategory><cac:TaxScheme>
        <cbc:ID>${t.scheme}</cbc:ID><cbc:Name>${TAX_NAMES[t.scheme][0]}</cbc:Name><cbc:TaxTypeCode>${TAX_NAMES[t.scheme][1]}</cbc:TaxTypeCode>
      </cac:TaxScheme></cac:TaxCategory>
    </cac:TaxSubtotal>`,
      )
      .join('\n    ')}
  </cac:TaxTotal>
  <cac:${monetary}>
    ${amount('LineExtensionAmount', lineExtension)}
    ${amount('AllowanceTotalAmount', allowance)}
    ${amount('ChargeTotalAmount', charge)}
    ${amount('PrepaidAmount', prepaid)}
    ${amount('PayableAmount', payable)}
  </cac:${monetary}>
</${root}>
`;
}

/** Constancia de recepción (CDR) mínima de SUNAT. */
export function buildCdr(id = 'F001-00001234') {
  return `<?xml version="1.0" encoding="UTF-8"?>
<ar:ApplicationResponse xmlns:ar="urn:oasis:names:specification:ubl:schema:xsd:ApplicationResponse-2"
  xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:ResponseDate>2026-10-15</cbc:ResponseDate>
  <cbc:Description>La Factura numero ${id}, ha sido aceptada</cbc:Description>
</ar:ApplicationResponse>
`;
}

export function writeUbl(filePath: string, xml: string, encoding: BufferEncoding = 'utf8'): string {
  fs.writeFileSync(filePath, Buffer.from(xml, encoding));
  return filePath;
}
