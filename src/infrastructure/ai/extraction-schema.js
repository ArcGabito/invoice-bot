import { z } from 'zod';

/**
 * Contrato de extracción que debe cumplir cualquier proveedor de IA.
 * Los importes se piden como texto decimal ("1234.56") para no depender de la
 * coma flotante; la normalización a céntimos se hace en el dominio.
 * Todos los campos son obligatorios pero admiten null: el modelo debe decir
 * explícitamente que no encontró un dato en lugar de omitirlo o inventarlo.
 */
const nullableText = (description) => z.string().nullable().describe(description);
const amount = (description) =>
  z
    .string()
    .nullable()
    .describe(
      `${description}. Texto decimal con punto y sin símbolo de moneda, p. ej. "1234.56". null si no aparece.`,
    );

export const ExtractionSchema = z.object({
  is_invoice: z
    .boolean()
    .describe(
      'true si el documento es un comprobante de pago (factura, boleta, nota de crédito/débito, ticket, recibo)',
    ),
  supplier_name: nullableText('Razón social o nombre del emisor'),
  supplier_ruc: nullableText('RUC del emisor, 11 dígitos sin espacios'),
  customer_name: nullableText('Razón social o nombre del cliente/adquiriente'),
  customer_ruc: nullableText('RUC (11 dígitos) o DNI (8 dígitos) del cliente'),
  document_type: nullableText(
    'Código SUNAT del tipo de comprobante: 01 factura, 03 boleta, 07 nota de crédito, 08 nota de débito, 12 ticket, 14 recibo de servicios públicos',
  ),
  series: nullableText('Serie del comprobante, p. ej. F001, B001, E001 o 0001'),
  number: nullableText('Número correlativo tal como aparece, p. ej. 00001234'),
  issue_date: nullableText('Fecha de emisión en formato AAAA-MM-DD'),
  due_date: nullableText('Fecha de vencimiento en formato AAAA-MM-DD'),
  currency: nullableText('Código ISO 4217 de la moneda: PEN para soles, USD para dólares'),
  subtotal: amount('Subtotal o valor de venta antes de impuestos'),
  taxable_amount: amount('Operaciones gravadas (base imponible del IGV)'),
  exempt_amount: amount('Operaciones exoneradas'),
  unaffected_amount: amount('Operaciones inafectas'),
  discount_amount: amount('Total de descuentos globales'),
  igv: amount('IGV'),
  isc: amount('ISC'),
  icbper: amount('Impuesto a las bolsas plásticas (ICBPER)'),
  other_charges: amount('Otros cargos'),
  other_taxes: amount('Otros tributos'),
  total: amount('Importe total a pagar del comprobante'),
  prepaid_amount: amount('Anticipos ya pagados y deducidos del total'),
  notes: z
    .array(z.string())
    .describe(
      'Observaciones breves sobre datos ilegibles, ambiguos o contradictorios. Lista vacía si no hay.',
    ),
});

export const EXTRACTION_FIELDS = Object.keys(ExtractionSchema.shape);
