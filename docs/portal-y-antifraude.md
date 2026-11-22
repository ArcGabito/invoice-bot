# Portal de proveedores, antifraude e indicadores

## Portal de proveedores

`https://facturas.empresa.pe/portal`: el proveedor consulta el estado de sus comprobantes y la fecha programada de pago, y sube sus XML y PDF. Así bajan las llamadas y correos de "¿cuándo me pagan?".

1. El proveedor escribe su RUC y su correo. Si el correo es un **contacto** de ese RUC, se publica el evento `portal.login_link` con un enlace de un solo uso (30 minutos) y **email-bot** lo envía. La respuesta es la misma si el correo no está registrado (no se puede averiguar quién es proveedor).
2. Con el enlace entra por 8 horas (cookie `HttpOnly`, `SameSite=Lax`, `Secure`). Solo ve los comprobantes de su RUC, sin datos internos: en revisión, en aprobación, pago programado (fecha e importe neto), pagado o rechazado (con el motivo).
3. Lo que sube se procesa como cualquier comprobante (`documents.scan`).

Contactos:

- Se **aprenden solos**: el correo desde el que llegó un comprobante queda como contacto de su proveedor.
- O se registran a mano: `invoicebot portal contact 20100047218 cobranzas@proveedor.pe` o `POST /api/v1/supplier-contacts`.

Configura `INVOICEBOT_PUBLIC_URL` para que los enlaces apunten a tu dominio. Para evitar abusos, el inicio de sesión tiene el mismo límite de intentos que la pantalla de revisión.

## Antifraude

Cada comprobante guardado pasa por estas señales antes del match. Si aparece alguna, el comprobante queda con una advertencia y no se aprueba solo:

| Advertencia | Cuándo |
| --- | --- |
| `FRAUD_NEW_SUPPLIER_HIGH_AMOUNT` | Primera factura del proveedor por encima de `INVOICEBOT_FRAUD_NEW_SUPPLIER_AMOUNT` (S/ 10 000) |
| `FRAUD_AMOUNT_OUT_OF_RANGE` | Con al menos 5 facturas previas, el importe supera la media más 3 desviaciones |
| `FRAUD_FUZZY_DUPLICATE` | Mismo proveedor, importe y fecha con otra serie o número (el mismo cobro dos veces) |

Se publica `invoice.fraud_alert` para avisar a tesorería. El cambio de cuenta bancaria de un proveedor lo controla bank-bot. Un RUC dado de baja lo marca la verificación en SUNAT (`SUNAT_RUC_*`).

## Indicadores

`GET /api/v1/analytics/kpis?month=2026-10` (y `invoicebot kpis --month 2026-10`):

| Indicador | Qué mide |
| --- | --- |
| `straight_through_rate` | Comprobantes autorizados sin intervención humana (match automático y aprobación automática) |
| `median_hours_to_authorize` | Mediana de horas entre la recepción y la autorización de pago |
| `median_hours_authorize_to_pay` | Mediana de horas entre la autorización y el pago |
| `ai_cost_usd` · `ai_cost_per_invoice_usd` | Tokens de IA por su precio (`INVOICEBOT_AI_PRICE_INPUT_PER_MTOK`, `_OUTPUT_PER_MTOK`) |
| `sunat_observed` · `rejected` · `from_xml` | Calidad de lo recibido |

Para Power BI, la vista **`v_invoice_lifecycle`** tiene una fila por comprobante con sus fechas (recepción, revisión, autorización, pago), si fue automático, los tokens de IA y los estados. Respeta la Row-Level Security: un usuario de SQL limitado a una empresa solo ve la suya.
