# Cuentas por pagar: órdenes de compra, match y aprobación de pago

```text
 ERP ── órdenes de compra ──┐        nota de ingreso / guía / conformidad ──┐
                            ▼                                                ▼
 comprobante ─► InvoiceBot ─► match a 2 o 3 vías ─► revisión ─► aprobación de pago ─► tesorería
                              (purchasing.match)    (contabilidad)  (matriz por niveles)   (bank-bot)
```

## 1. Órdenes de compra y recepciones

Los importes van **sin IGV**. Una orden sin empresa sirve para cualquier empresa (instalaciones de una sola empresa); con empresa, solo para esa.

Por la API (desde el ERP):

```bash
curl -X POST https://facturas.empresa.pe/api/v1/purchase-orders -H "Authorization: Bearer $KEY" \
  -H 'content-type: application/json' -d '{
    "po_number": "OC-2026-001", "supplier_ruc": "20100047218", "company_ruc": "20601234565",
    "cost_center": "ADM", "requires_receipt": true,
    "lines": [{"line_no": 1, "item_code": "PAP-A4", "quantity": 10, "unit_price": 10.00}]
  }'

curl -X POST https://facturas.empresa.pe/api/v1/goods-receipts -H "Authorization: Bearer $KEY" \
  -H 'content-type: application/json' -d '{
    "receipt_number": "NI-778", "supplier_ruc": "20100047218", "po_number": "OC-2026-001",
    "despatch_reference": "T001-55", "lines": [{"line_no": 1, "quantity": 10}]
  }'
```

O por CSV (una fila por línea):

```bash
invoicebot purchasing import-orders ordenes.csv     # company_ruc,po_number,supplier_ruc,currency,issue_date,cost_center,line_no,item_code,description,quantity,unit_price
invoicebot purchasing import-receipts recepciones.csv  # company_ruc,receipt_number,supplier_ruc,receipt_date,po_number,despatch_reference,line_no,quantity
invoicebot purchasing orders --supplier 20100047218
```

Una conformidad de servicio sin líneas lleva `amount` (valor sin IGV). Las guías de remisión electrónicas leídas por document-bot pueden llegar como recepciones por esta misma API.

## 2. Match

Cada comprobante guardado pasa por la tarea `purchasing.match`:

1. **Notas de crédito (07) y débito (08)** se aplican a la factura que citan (`BillingReference` del XML). El saldo de cada factura está en la vista `v_invoice_balances`.
2. **Orden de compra**: la que cita el XML (`OrderReference`), la de la guía que cita (`DespatchDocumentReference` = `despatch_reference` de una recepción) o, si el proveedor tiene una sola orden abierta con saldo suficiente, esa. También se vincula a mano: `invoicebot purchasing match <factura> --po OC-2026-001` o `POST /api/v1/invoices/{id}/match`.
3. **Match a 3 vías** si la orden exige recepción (`requires_receipt`), a 2 vías si no. Tolerancia: `INVOICEBOT_MATCH_TOLERANCE_PERCENT` (1 %) con un mínimo de `INVOICEBOT_MATCH_TOLERANCE_AMOUNT` (S/ 10).

| Excepción | Qué significa |
| --- | --- |
| `PO_NOT_RECEIVED` | La orden exige recepción y no hay ninguna |
| `PO_RECEIPT_EXCEEDED` | La factura supera lo recibido pendiente de facturar |
| `PO_AMOUNT_EXCEEDED` | La factura supera lo que queda por facturar de la orden |
| `PO_SUPPLIER_MISMATCH` · `PO_CURRENCY_MISMATCH` · `PO_NOT_OPEN` | Proveedor, moneda o estado de la orden |

Al registrar una recepción, las facturas de esa orden que no coincidían se vuelven a evaluar solas.

4. **Datos contables**: el centro de costo de la orden y la cuenta contable que contabilidad usó antes para ese proveedor (`PATCH /api/v1/invoices/{id}/accounting` aprende de cada corrección).
5. Con `INVOICEBOT_AUTO_APPROVE_MATCHED=true`, una factura **válida** que **coincide** con su orden aprueba la revisión sola (firma `invoicebot:match`): es el procesamiento sin intervención (STP).

## 3. Aprobación de pago

Empieza cuando la revisión queda aprobada. La **matriz** define niveles por monto (en soles, con el tipo de cambio de la factura), empresa, centro de costo y proveedor; por nivel gana la regla más específica.

```bash
# Hasta S/ 5 000: automático si coincide con la OC; si no, lo aprueba el jefe.
invoicebot approvals rule-add --level 1 --max 5000 --escalate-to jefe
# Desde S/ 5 000: jefe y luego gerente (SLA 24 h; si vence, pasa a la gerencia general).
invoicebot approvals rule-add --level 1 --min 5000 --approver jefe --sla-hours 24 --escalate-to gerente
invoicebot approvals rule-add --level 2 --min 5000 --approver gerente --escalate-to gerencia
invoicebot approvals rules
invoicebot approvals delegate jefe --to asistente --from 2026-12-20 --until 2027-01-05 --reason vacaciones
```

- Cada solicitud genera el evento `approval.requested` con un **enlace de un clic** (`INVOICEBOT_PUBLIC_URL/approve/<token>`, un solo uso, 7 días): email-bot lo envía por correo y la persona aprueba o rechaza sin iniciar sesión.
- También desde la API (`POST /api/v1/approvals/{id}/approve|reject`, rol `approver`) o la CLI.
- `approvals.remind` (cada hora, `INVOICEBOT_SCHEDULE_APPROVALS`) recuerda una vez lo vencido, con un enlace nuevo, y si sigue sin respuesta un día después lo escala a `escalate_to`.
- Estado final en `invoices.approval_status`: `approved` o `not_required` (se puede pagar) o `rejected`.

## 4. Eventos y webhooks

Los eventos se guardan en la misma transacción que el cambio (`outbox_events`) y se entregan a cada webhook suscrito con reintentos (15 s, 30 s, 1 min… hasta 8 intentos).

```bash
invoicebot webhook add email-bot https://email-bot.empresa.pe/hooks/invoicebot --events "approval.*,invoice.*"
invoicebot webhook events
```

| Evento | Cuándo |
| --- | --- |
| `invoice.matched` · `invoice.match_exception` | Resultado del match |
| `invoice.note_applied` | Nota de crédito o débito aplicada |
| `approval.requested` · `approval.reminder` · `approval.escalated` | Flujo de aprobación (incluyen `approve_url`) |
| `invoice.approved_for_payment` · `invoice.payment_rejected` | Resultado de la aprobación |

El cuerpo sigue CloudEvents 1.0. Para verificar la firma en el receptor:

```python
import hashlib, hmac

def valid(secret: str, headers, body: bytes) -> bool:
    stamp = headers["X-InvoiceBot-Timestamp"]
    expected = "sha256=" + hmac.new(secret.encode(), stamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(headers["X-InvoiceBot-Signature"], expected)
```

Usa `X-InvoiceBot-Delivery` para no procesar dos veces la misma entrega y rechaza marcas de tiempo de más de 5 minutos.
