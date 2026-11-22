# Cumplimiento tributario: SIRE, detracciones, retenciones y plazos

> Los porcentajes, montos mínimos y formatos de archivo son los vigentes a la fecha de esta versión (ver `domain/tax.py`). SUNAT los cambia por resolución: revísalos antes del cierre.

## SIRE: cuadre del Registro de Compras

1. En SUNAT Operaciones en Línea → SIRE → Registro de Compras → **Propuesta**, descarga la exportación del periodo (TXT o ZIP).
2. Impórtala y crúzala con lo registrado:

```bash
invoicebot sire import propuesta-202610.zip --period 2026-10 [--company 20601234565]
invoicebot sire reconcile --period 2026-10 --xlsx diferencias-202610.xlsx
```

O por la API: `POST /api/v1/sire/proposals` (multipart `file`, `period`) y `GET /api/v1/sire/reconciliation?period=2026-10&format=xlsx`.

La lectura usa los **encabezados** de la exportación (RUC del proveedor, tipo, serie, número, fecha, IGV, total, CAR), no posiciones fijas: sigue funcionando si SUNAT agrega columnas. Los números se comparan sin ceros a la izquierda.

| Resultado | Qué hacer |
| --- | --- |
| Coinciden | Nada: listos para aceptar la propuesta |
| Solo en SUNAT | Pedir el comprobante al proveedor (o buscarlo en el correo) |
| Solo en InvoiceBot | Revisar: otro periodo, anulado o aún no informado por el proveedor |
| Total, IGV o fecha distintos | Corregir el registro o pedir una nota al proveedor |

> La descarga automática por el API SIRE (credenciales de SUNAT y tickets) está pendiente: se agregará con el manual vigente del API y una cuenta de pruebas.

## Reverificación antes del cierre

La tarea `sunat.reverify` (día 3 de cada mes a las 7:00, `INVOICEBOT_SCHEDULE_REVERIFY`) vuelve a consultar en SUNAT todos los comprobantes del mes anterior: detecta los anulados después de recibidos y los RUC que pasaron a no habido.

## Detracciones (SPOT)

```bash
invoicebot detractions pending                         # sujetas a detracción, sin constancia
invoicebot detractions payment-file 120 121 --lot 260001 -o D260001.txt [--company RUC]
invoicebot detractions deposit 120 --constancia 123456789 --date 2026-11-05 --amount 170
```

- El depósito va en **soles enteros** (redondeo sin decimales).
- El archivo de depósito masivo es de ancho fijo: cabecera del adquirente y una línea por comprobante con la cuenta del Banco de la Nación, el código del bien o servicio, el periodo y el comprobante. **Verifica el layout con la guía vigente de SUNAT antes de subirlo**; está aislado en `domain/tax.py::spot_payment_file`.
- `compliance.alerts` avisa (evento `detraction.pending`) las facturas autorizadas para pago con la detracción sin depositar.

## Retenciones del IGV (3 %)

Si la empresa es agente de retención (`invoicebot company add … --retention-agent`), el reporte calcula el 3 % de cada factura autorizada para pago:

```bash
invoicebot taxpayers import-padron buenos-contribuyentes.txt --kind good
invoicebot taxpayers import-padron agentes-retencion.txt --kind retention
invoicebot taxpayers set 20100047218 --perception-agent
invoicebot taxpayers retentions --month 2026-10 --company 20601234565
```

No se retiene si: la operación está sujeta a detracción, el proveedor es agente de retención, de percepción o buen contribuyente, el comprobante no es factura ni nota de débito, o el importe no supera S/ 700.

## Plazo del crédito fiscal

Un comprobante debe anotarse en el Registro de Compras dentro de los 12 periodos siguientes al de su emisión. `compliance.alerts` (días hábiles a las 8:00, `INVOICEBOT_SCHEDULE_COMPLIANCE`) publica `tax.annotation_deadline` para los comprobantes a menos de 60 días del límite que aún no aparecen en ninguna propuesta del SIRE importada. email-bot puede avisar a contabilidad con ese evento.
