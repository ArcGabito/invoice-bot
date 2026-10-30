# Changelog

Formato basado en [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/). El proyecto sigue [Semantic Versioning](https://semver.org/lang/es/).

## [Sin publicar]

### Añadido

- Lectura de comprobantes electrónicos SUNAT en XML UBL 2.1 (factura, boleta, nota de crédito y de débito) sin IA, con reparto de importes según el catálogo 05 de tributos y soporte de UTF-8 e ISO-8859-1.
- Vinculación del PDF con el mismo nombre que un XML ya extraído (representación impresa): no se envía a la IA.
- Reconocimiento de la constancia de recepción (CDR) de SUNAT y de XML que no son comprobantes: se omiten con su motivo.
- Columna `data_source` (`xml` o `ai`) en facturas y en el CSV; al exportar posibles duplicados se prefiere el XML.
- Copia de seguridad automática en `data/backups/` antes de migrar una base de datos con datos.
- `npm run samples` genera comprobantes ficticios, incluido un XML con su PDF y su CDR.

### Cambiado

- Migración 002: nuevo estado de documento `skipped`, método `xml` y campos `skip_reason` y `linked_document_id`. Las migraciones se ejecutan con las claves foráneas desactivadas y verifican la integridad al terminar.

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
