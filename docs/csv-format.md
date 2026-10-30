# Formato del CSV contable

`invoicebot export` genera un CSV en UTF-8, con fin de línea CRLF, una fila de encabezados y una fila por factura. Por defecto usa coma como separador (`--delimiter ";"` para Excel en español) y no añade BOM (`--bom` para Excel).

Las columnas, su orden y sus nombres son estables dentro de una versión mayor. Las nuevas columnas se añaden al final.

| Columna | Formato | Descripción |
| --- | --- | --- |
| `invoice_id` | entero | Identificador interno de la factura. |
| `issue_date` | `AAAA-MM-DD` | Fecha de emisión. |
| `due_date` | `AAAA-MM-DD` o vacío | Fecha de vencimiento. |
| `supplier_ruc` | 11 dígitos | RUC del emisor. |
| `supplier_name` | texto | Razón social del emisor. |
| `customer_ruc` | 11 u 8 dígitos o vacío | RUC o DNI del cliente. |
| `customer_name` | texto | Nombre del cliente. |
| `document_type` | 2 dígitos | Catálogo 01 de SUNAT: `01` factura, `03` boleta, `07` nota de crédito, `08` nota de débito. |
| `series` | texto | Serie (`F001`, `B001`, `E001`, `0001`). |
| `number` | dígitos | Correlativo, tal como aparece en el comprobante. |
| `currency` | ISO 4217 | `PEN`, `USD`… |
| `subtotal` | decimal | Subtotal o valor de venta. |
| `taxable_amount` | decimal | Operaciones gravadas. |
| `exempt_amount` | decimal | Operaciones exoneradas. |
| `unaffected_amount` | decimal | Operaciones inafectas. |
| `discount_amount` | decimal | Descuentos globales. |
| `igv` | decimal | IGV. |
| `isc` | decimal | ISC. |
| `icbper` | decimal | Impuesto a las bolsas plásticas. |
| `other_charges` | decimal | Otros cargos. |
| `other_taxes` | decimal | Otros tributos. |
| `total` | decimal | Importe total. |
| `validation_status` | `valid`, `warning`, `error` | Resultado de la validación. |
| `duplicate_count` | entero | Otras facturas registradas con el mismo emisor, tipo, serie y número. |
| `file_name` | texto | Nombre del archivo original. |
| `sha256` | hex | Huella del archivo original. |
| `data_source` | `xml`, `ai` | Origen de los datos: el XML del comprobante electrónico (exacto) o la extracción con IA desde el PDF (propuesta, validada). Añadida en v0.2.0. |

**Importes**: punto como separador decimal, dos decimales, sin separador de miles ni símbolo de moneda (`1234.50`). Un campo vacío significa que el dato no aparece en el comprobante; no equivale a cero.

**Filtros por defecto**: solo facturas `valid` y `warning`; de cada grupo de posibles duplicados se exporta la leída de un XML si existe y, si no, la primera registrada. Los documentos fallidos o pendientes de OCR no tienen factura y nunca aparecen.
