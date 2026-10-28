# Arquitectura

InvoiceBot es una aplicación local de un solo proceso, organizada en capas. La regla fundamental: **el dominio no importa SDKs de IA, bibliotecas PDF ni SQLite**, así que las reglas contables no cambian cuando cambian las herramientas.

```text
src/
├── cli/              Presentación: Commander, formato de salida, códigos de salida
│   ├── index.js        programa y traducción de errores
│   ├── context.js      raíz de composición (crea las dependencias reales)
│   └── commands/
├── application/      Casos de uso (orquestan, no conocen detalles técnicos)
│   ├── use-cases/      scan, process-document, retry, export, duplicates, review
│   └── ports/          contratos (JSDoc) que implementa la infraestructura
├── domain/           Reglas de negocio puras: normalización, validación, RUC, duplicados
├── infrastructure/   Adaptadores
│   ├── database/       SQLite (better-sqlite3), migraciones SQL, repositorio
│   ├── documents/      descubrimiento de archivos, SHA-256, texto de PDF (pdfjs-dist)
│   ├── ai/             esquema de extracción (Zod), prompt versionado, proveedores
│   ├── exports/        CSV
│   └── logging/        pino → stderr
└── shared/           configuración, errores, importes en céntimos, concurrencia
```

## Base de datos

| Tabla | Contenido |
| --- | --- |
| `documents` | Un registro por contenido único (SHA-256): ruta original, estado, intentos, último error. |
| `file_occurrences` | Cada ruta donde se vio un documento. Varias rutas = duplicado exacto. |
| `extraction_runs` | Historial de cada llamada a la IA: proveedor, modelo, versión del prompt, tokens, respuesta cruda, error. |
| `invoices` | Factura vigente por documento, importes en céntimos, clave de negocio, estado de validación, versión de reglas. |
| `validation_issues` | Cada error o advertencia, con valor esperado y encontrado. |
| `schema_migrations` | Migraciones aplicadas. |

Estados de un documento: `pending` → `processing` → `extracted` | `failed` | `needs_ocr`. Un `processing` al iniciar una ejecución significa que la anterior se interrumpió, y vuelve a `pending`.

La clave de negocio (`RUC|tipo|serie|número`) tiene índice pero no restricción de unicidad: los datos extraídos pueden ser erróneos y una restricción rechazaría justo los casos que hay que revisar.

## Añadir un proveedor de IA

1. Crea `src/infrastructure/ai/providers/<nombre>-provider.js` con `name`, `model`, `extract({ text, fileName })` y `ping()` (ver `src/application/ports/ai-provider.js`).
2. `extract` debe devolver datos que cumplan `ExtractionSchema` y lanzar `ExtractionError` con `kind` y `retryable` adecuados.
3. Regístralo en `provider-factory.js` y en el enum de `src/shared/config.js`.

## Añadir una migración

Crea `src/infrastructure/database/migrations/NNN_descripcion.sql`. Se aplica automáticamente, en una transacción, al abrir la base de datos.
