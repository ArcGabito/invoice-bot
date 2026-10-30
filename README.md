# InvoiceBot

![Wallpaper](https://media.gabito.dev/imagenes/1793201727477-invoice-bot-wallpaper.png)

Automatización inteligente de facturas mediante extracción de texto, LLM y validación contable local.

InvoiceBot es una CLI para Windows (y cualquier sistema con Node.js 24) que lee comprobantes de pago peruanos, extrae sus datos con un modelo de IA, **comprueba con código determinista** que sean coherentes, detecta duplicados y guarda todo en una base de datos SQLite local, con trazabilidad de cada resultado.

> El LLM propone los datos; el código comprueba su consistencia; el usuario resuelve los casos dudosos.

## Estado: v0.1.0

| Funcionalidad | Estado |
| --- | --- |
| Escaneo de carpetas (PDF, PNG, JPG), SHA-256 e idempotencia | ✅ |
| Extracción de texto nativo de PDF | ✅ |
| Extracción con IA: Claude (Anthropic), Ollama (local) o simulado | ✅ |
| Validación: RUC (módulo 11), fechas, moneda, serie, aritmética tributaria, IGV | ✅ |
| Duplicados exactos (SHA-256) y posibles (emisor + tipo + serie + número) | ✅ |
| Persistencia SQLite con migraciones, historial de intentos e incidencias | ✅ |
| Reintentos y recuperación de ejecuciones interrumpidas | ✅ |
| Exportación CSV con filtros | ✅ |
| OCR para PDFs escaneados e imágenes | ⏳ v0.2.0 — hoy quedan en estado `needs_ocr` |
| Revisión manual interactiva y resolución de incidencias | ⏳ v0.3.0 |

## Instalación

Requisitos: [Node.js 24 LTS](https://nodejs.org/) (`node --version` debe mostrar v24 o superior).

```bash
git clone https://github.com/ArcGabito/invoice-bot.git
cd invoice-bot
npm install
copy .env.example .env
```

Edita `.env` y elige un proveedor de IA:

- **Claude (Anthropic)** — `INVOICEBOT_AI_PROVIDER=anthropic` y `ANTHROPIC_API_KEY=...` (o inicia sesión con `ant auth login`). Usa salida estructurada validada con un esquema y el modelo `claude-opus-5-5` por defecto. Tiene activado el *fallback* del lado del servidor: si el modelo rechaza un documento, la API lo reintenta con otro modelo en la misma llamada.
- **Ollama (local)** — `INVOICEBOT_AI_PROVIDER=ollama`. Los documentos no salen del equipo. Instala [Ollama](https://ollama.com/) y descarga un modelo (`ollama pull qwen3:8b`).
- **Simulado** — `INVOICEBOT_AI_PROVIDER=mock`. Extracción por expresiones regulares, sin IA ni coste. Útil para probar el flujo.

Comprueba la instalación:

```bash
npm start -- doctor
```

Para tener el comando `invoicebot` disponible globalmente:

```bash
npm link
```

## Uso

```text
$ invoicebot scan ./facturas

InvoiceBot v0.1.0

Documentos encontrados: 24
Procesados correctamente: 19
Con advertencias: 3
Con errores: 1
Fallidos: 0
Requieren OCR: 1
Ya procesados (omitidos): 0
Duplicados detectados: 1 (exactos: 1, posibles: 0)

Tiempo total: 00:01:42
Base de datos: C:\...\invoice-bot\data\invoicebot.db
```

| Comando | Función |
| --- | --- |
| `invoicebot scan ./facturas` | Procesa los documentos de una carpeta (o un archivo). |
| `invoicebot scan ./facturas --recursive` | Incluye subcarpetas. |
| `invoicebot scan ./facturas --dry-run` | Inspecciona los archivos sin guardar nada ni llamar a la IA. |
| `invoicebot status` | Estado de la base de datos, documentos pendientes y consumo de tokens. |
| `invoicebot export --month 2026-10 --csv` | Exporta las facturas del mes a `exports/invoices-2026-10.csv`. |
| `invoicebot duplicates` | Lista duplicados exactos y posibles coincidencias. |
| `invoicebot review` | Facturas con errores o advertencias, documentos fallidos y pendientes de OCR. |
| `invoicebot retry --failed` | Reintenta documentos fallidos (errores transitorios o salidas inválidas). |
| `invoicebot retry --pending` | Recupera documentos de una ejecución interrumpida. |
| `invoicebot doctor` | Comprueba Node.js, SQLite, lectura de PDF, OCR y conexión con la IA. |
| `invoicebot config` | Muestra la configuración efectiva sin revelar secretos. |

Opciones globales: `--provider <anthropic|ollama|mock>`, `--data-dir <ruta>`, `--concurrency <n>`, `--json` (salida para automatización) y `--verbose`.

### Exportación

```bash
invoicebot export --month 2026-10 --supplier 20100047218 --currency PEN --bom
```

Por defecto se exportan las facturas **válidas y con advertencias**, y se excluyen las repeticiones de un mismo comprobante. Usa `--include-errors`, `--include-duplicates` o `--status valid` para cambiarlo, y `--bom --delimiter ";"` si vas a abrir el archivo en Excel con configuración regional en español. El formato de columnas está documentado en [docs/csv-format.md](docs/csv-format.md).

### Códigos de salida

| Código | Significado |
| --- | --- |
| `0` | Operación completada. |
| `1` | Error general (p. ej., `doctor` encontró un problema). |
| `2` | Argumentos o configuración incorrectos. |
| `3` | Ejecución parcial: hubo documentos fallidos o facturas con errores de validación. |

## Cómo funciona

```text
scan → descubrir archivos → SHA-256 → ¿ya procesado? → sí: omitir
                                         ↓ no
                            texto del PDF → ¿suficiente? → no: needs_ocr
                                         ↓ sí
                            LLM (JSON con esquema) → normalizar → validar
                                         ↓
                            duplicados de negocio → SQLite (transacción) → resumen
```

- **Idempotencia**: cada archivo se identifica por su SHA-256. Ejecutar dos veces el mismo comando no duplica registros ni repite llamadas a la IA.
- **Concurrencia limitada**: `INVOICEBOT_CONCURRENCY` documentos a la vez (2 por defecto).
- **Reintentos controlados**: un JSON inválido se repite una vez en el momento; los errores de red se reintentan con `retry --failed`; un rechazo del modelo, un documento que no es comprobante o un PDF dañado no se reintentan salvo con `--force`.
- **Trazabilidad**: cada intento queda en `extraction_runs` con proveedor, modelo que respondió, versión del prompt, tokens y respuesta cruda; cada factura guarda la versión de las reglas de validación.

### Reglas de validación

Las incidencias se guardan individualmente en `validation_issues`. Una factura con cualquier error queda como `error`; con advertencias, como `warning`; solo sin incidencias es `valid`.

| Regla | Severidad |
| --- | --- |
| Campos obligatorios: RUC emisor, tipo, serie, número, fecha, moneda, total | error |
| RUC del emisor: 11 dígitos, prefijo 10/15/16/17/20 y dígito de control | error |
| Fecha de emisión inexistente (p. ej. 30 de febrero) | error |
| Total ≠ base (gravada + exonerada + inafecta, o subtotal) + IGV + ISC + ICBPER + otros, más allá de la tolerancia | error |
| Importe ilegible o negativo; moneda que no es ISO 4217 | error |
| IGV que no corresponde al 18 % ni al 10,5 % de la base gravada | advertencia |
| Fecha futura, serie con formato no SUNAT o de otro tipo de comprobante, RUC del cliente inválido | advertencia |
| Posible duplicado de otro documento | advertencia |

El IGV no se calcula sobre todo el subtotal cuando hay operaciones exoneradas o inafectas: se usa el desglose tributario del comprobante. La aritmética se hace con enteros en céntimos y una tolerancia explícita (`INVOICEBOT_AMOUNT_TOLERANCE`, 0.02 por defecto).

> Validar matemáticamente un RUC **no** confirma que la empresa exista, esté activa ni que el comprobante sea auténtico. Eso requiere consultar fuentes autorizadas (SUNAT) y será una funcionalidad independiente.

## Seguridad y privacidad

- `.env`, `data/`, `exports/`, logs y archivos PDF/PNG/JPG están en `.gitignore`: no subas facturas reales.
- Las claves de API se leen solo de variables de entorno y `invoicebot config` nunca las muestra.
- Los logs van a stderr y no incluyen el texto de las facturas.
- El contenido de cada documento se envía al modelo delimitado como datos, con instrucciones explícitas de ignorar cualquier orden que contenga.
- Con `INVOICEBOT_AI_PROVIDER=ollama`, ningún documento sale del equipo.

## Desarrollo

```bash
npm test          # Vitest: unitarias, integración (SQLite + PDF) y CLI de extremo a extremo
npm run lint      # ESLint
npm run format    # Prettier
npm run samples   # crea comprobantes ficticios en ./facturas-demo para probar la CLI
```

Las pruebas generan sus propios PDFs ficticios en directorios temporales; no necesitan facturas reales ni conexión. El proyecto incluye `invoice-bot.slnx` para abrirlo en Visual Studio y configuraciones de depuración en `.vscode/launch.json`.

## Licencia

[MIT](LICENSE.md)
