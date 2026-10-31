# Changelog

Formato basado en [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/). El proyecto sigue [Semantic Versioning](https://semver.org/lang/es/).

## [0.2.0] - 2026-10-30

### Añadido

- Lectura de comprobantes electrónicos SUNAT en XML UBL 2.1 (factura, boleta, nota de crédito y de débito) sin IA, con reparto de importes según el catálogo 05 de tributos y soporte de UTF-8 e ISO-8859-1.
- Vinculación del PDF con el mismo nombre que un XML ya extraído (representación impresa): no se envía a la IA.
- Reconocimiento de la constancia de recepción (CDR) de SUNAT y de XML que no son comprobantes: se omiten con su motivo.
- Columna `data_source` (`xml` o `ai`) en facturas y en el CSV; al exportar posibles duplicados se prefiere el XML.
- Copia de seguridad automática en `data/backups/` antes de migrar una base de datos con datos.
- `npm run samples` genera comprobantes ficticios, incluido un XML con su PDF y su CDR.
- Comando `verify-sunat`: consulta el servicio oficial de SUNAT (Consulta Integrada de Comprobante de Pago) para saber si cada comprobante está aceptado, anulado o no informado, y si el RUC emisor está activo y habido. Las respuestas generan incidencias `SUNAT_*` que recalculan el estado de la factura; cada consulta queda en `sunat_checks`.
- Columna `sunat_status` en facturas y en el CSV.
- `doctor` y `config` muestran la configuración de la API SUNAT (opcional).
- Verificación en SUNAT configurable: `INVOICEBOT_SUNAT_ENABLED` (interruptor general, desactivado por defecto), `INVOICEBOT_SUNAT_VERIFY_ON_SCAN` (verificar al terminar `scan`; anulable con `scan --no-verify-sunat` o forzable con `--verify-sunat`), tiempo de espera, pausa entre consultas y URLs propias para proxy o entorno de pruebas.
- Exportación a Excel (`export --xlsx` o `--format xlsx`, por defecto con `INVOICEBOT_EXPORT_FORMAT`): hoja *Facturas* con importes numéricos, fechas reales y RUC/serie/número como texto, y hoja *Resumen* por moneda y estado. Usa `write-excel-file` (mantenido, una sola dependencia, sin vulnerabilidades conocidas).
- Comando `run`: ciclo desatendido que reintenta fallos temporales, escanea `INVOICEBOT_INBOX_DIR`, verifica en SUNAT si está configurado, exporta el mes actual y el anterior (`INVOICEBOT_RUN_EXPORT`) y hace copia de seguridad cuando toca. `run --watch` lo repite cada `INVOICEBOT_RUN_INTERVAL_MINUTES`. Candado de una sola ejecución con recuperación de candados abandonados e historial en la tabla `bot_runs`, visible en `status`.
- `scripts/windows/register-scheduled-task.ps1`: registra o elimina la tarea programada de Windows (`-WhatIf` para simular).
- Comando `backup`: copia con `VACUUM INTO`, verificada con `integrity_check`, y retención configurable (`INVOICEBOT_BACKUP_*`).
- Logs en archivo, uno por día, con borrado tras `INVOICEBOT_LOG_RETENTION_DAYS` (`INVOICEBOT_LOG_DIR`).
- Columna `prepaid_amount` (anticipos) en facturas, CSV y Excel. Migración 004.
- Revisión humana: comandos `show`, `approve`, `reject`, `reopen`, `correct` y `resolve`, y pantalla web local `serve` (bandeja, datos editables, incidencias, documento original e historial). Aprobar con errores exige `--force` y una nota; rechazar exige un motivo; corregir revalida y recalcula duplicados. Auditoría completa en `review_actions` (migración 006), columna `review_status` en las exportaciones y opción global `--user`. Las facturas rechazadas no se exportan ni cuentan como duplicados.
- Facturas de proveedores del extranjero: tipo 91 (no domiciliado), columnas `supplier_tax_id` y `supplier_country` (migración 005), validaciones propias (sin RUC, serie SUNAT ni IGV; nota informativa sobre sus obligaciones), duplicados por identificador fiscal o nombre y exclusión de la verificación en SUNAT. Probado con 13 facturas reales de Cloudflare.
- Fechas con el mes en letras, en inglés o en español ("August 5, 2025", "5 de agosto de 2025"), y nombres de país convertidos a código ISO.

### Corregido

- Lectura de PDF: los guiones sin texto Unicode (frecuentes en facturas generadas con Stripe) llegaban como U+0000 y el número `IN-45946823` se leía `IN   45946823`; ahora se recuperan entre letras o dígitos y se descartan en otras posiciones. Los fragmentos de texto se unen según su posición en la página, sin espacios donde no los hay.
- Un comprobante con datos completos ya no se descarta solo porque el modelo responda `is_invoice=false`: se procesa con la advertencia `MODEL_DOUBTED_INVOICE` (caso real: una boleta electrónica bien leída).
- Prompt `2026-10-31.1`: facturas del exterior, `$0.00` como "0.00" y no null, y nunca texto en campos de importe.

- Las facturas con anticipos ya no dan un error falso de total: el XML trae `PrepaidAmount`, la IA lo extrae como `prepaid_amount` y la validación acepta el total con el anticipo descontado (o ya restado de la base, según el emisor).

- Ollama, tras probarlo con un modelo local real (`qwen3.5:4b`): los modelos con razonamiento tardaban más de 5 minutos y devolvían una respuesta vacía. Ahora se envía `think: false` (configurable con `INVOICEBOT_OLLAMA_THINK`), un contexto de 8192 tokens (`INVOICEBOT_OLLAMA_NUM_CTX`) y la respuesta se recibe en streaming, porque `fetch` de Node cortaba a los 5 minutos sin cabeceras. Resultado: unos 10 s por factura. Los mensajes de error explican si la respuesta quedó truncada o si el modelo solo razonó.
- Prompt de extracción `2026-10-30.1`: indica explícitamente cómo separar serie y número y que el tipo de comprobante es un código de dos dígitos; el modelo pequeño confundía la serie con el tipo.
- Modelo de Ollama por defecto: `qwen3.5:4b`, `INVOICEBOT_OLLAMA_TIMEOUT_MS` configurable y `doctor` lista los modelos disponibles si falta el configurado.

- Entre posibles duplicados sin XML, la exportación conserva el del primer documento registrado y no el que terminó antes de procesarse; antes el resultado podía variar según la concurrencia.

### Cambiado

- Migración 002: nuevo estado de documento `skipped`, método `xml` y campos `skip_reason` y `linked_document_id`. Las migraciones se ejecutan con las claves foráneas desactivadas y verifican la integridad al terminar.
- Migración 003: tabla `sunat_checks` y columnas `sunat_status` y `sunat_checked_at` en facturas. Volver a extraer una factura borra su estado de SUNAT para que se consulte de nuevo.

## [0.1.0] - 2026-10-28

### Añadido

- CLI `invoicebot` con los comandos `scan`, `status`, `export`, `duplicates`, `review`, `retry`, `doctor` y `config`, salida `--json` y códigos de salida documentados.
- Descubrimiento de PDF, PNG y JPG (opcionalmente recursivo), SHA-256 por streaming e idempotencia por contenido.
- Extracción de texto nativo de PDF con pdfjs-dist; los documentos sin texto quedan en `needs_ocr`.
- Extracción con IA mediante un esquema Zod: Claude (salida estructurada y fallback del servidor), Ollama local y proveedor simulado.
- Normalización de importes a céntimos sin coma flotante, fechas DD/MM/AAAA y monedas.
- Validación de RUC (módulo 11), fechas, moneda, serie, aritmética tributaria (gravadas, exoneradas, inafectas, descuentos, ICBPER) e IGV al 18 % o 10,5 %.
- Duplicados exactos (SHA-256 en varias rutas) y posibles (emisor, tipo, serie y número).
- SQLite con migraciones, WAL, claves foráneas, historial de extracciones e incidencias individuales.
- Reintentos limitados y recuperación de ejecuciones interrumpidas.
- Exportación CSV con filtros por mes, estado, proveedor y moneda.
- Pruebas unitarias, de integración y de CLI con Vitest; CI en GitHub Actions para Windows y Linux.
