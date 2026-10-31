# InvoiceBot

![Wallpaper](https://media.gabito.dev/imagenes/1793201727477-invoice-bot-wallpaper.png)

Automatización inteligente de facturas mediante extracción de texto, LLM y validación contable local.

InvoiceBot es una CLI para Windows (y cualquier sistema con Node.js 24) que lee comprobantes de pago peruanos —directamente del XML del comprobante electrónico cuando existe, o con un modelo de IA desde el PDF—, **comprueba con código determinista** que sean coherentes, detecta duplicados y guarda todo en una base de datos SQLite local, con trazabilidad de cada resultado.

> El XML o el LLM proponen los datos; el código comprueba su consistencia; el usuario resuelve los casos dudosos.

## Estado: v0.2.0

| Funcionalidad | Estado |
| --- | --- |
| Escaneo de carpetas (XML, PDF, PNG, JPG), SHA-256 e idempotencia | ✅ |
| Lectura de comprobantes electrónicos SUNAT en XML UBL 2.1, sin IA | ✅ |
| Vinculación del PDF (representación impresa) con su XML; omisión de CDR | ✅ |
| Verificación en SUNAT: comprobante aceptado/anulado/no existe y RUC emisor activo/habido | ✅ |
| Extracción de texto nativo de PDF | ✅ |
| Extracción con IA: Claude (Anthropic), Ollama (local) o simulado | ✅ |
| Validación: RUC (módulo 11), fechas, moneda, serie, aritmética tributaria, IGV | ✅ |
| Duplicados exactos (SHA-256) y posibles (emisor + tipo + serie + número) | ✅ |
| Persistencia SQLite con migraciones, historial de intentos e incidencias | ✅ |
| Reintentos y recuperación de ejecuciones interrumpidas | ✅ |
| Exportación a CSV y Excel (.xlsx) con filtros | ✅ |
| Ejecución desatendida (`run`), tarea programada de Windows, historial de ejecuciones | ✅ |
| Copias de seguridad verificadas con retención y logs diarios en archivo | ✅ |
| Facturas con anticipos | ✅ |
| Facturas de proveedores del extranjero (no domiciliados, tipo 91): AWS, Google, Cloudflare… | ✅ |
| OCR para PDFs escaneados e imágenes | ⏳ próxima versión — hoy quedan en estado `needs_ocr` |
| Revisión humana: pantalla web local y comandos para aprobar, rechazar y corregir, con auditoría | ✅ |

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
- **Ollama (local)** — `INVOICEBOT_AI_PROVIDER=ollama`. Los documentos no salen del equipo. Instala [Ollama](https://ollama.com/) y descarga un modelo (`ollama pull qwen3.5:4b`). Probado con `qwen3.5:4b`: unos 10 s por factura y todos los campos correctos en los comprobantes de ejemplo. InvoiceBot desactiva por defecto el razonamiento del modelo (`INVOICEBOT_OLLAMA_THINK=false`), que lo hacía más de 30 veces más lento, y usa un contexto de 8192 tokens (`INVOICEBOT_OLLAMA_NUM_CTX`).
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

InvoiceBot v0.2.0

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
| `invoicebot verify-sunat` | Consulta en SUNAT si cada comprobante es válido y el estado del RUC emisor. |
| `invoicebot run` | Ciclo desatendido: reintenta, escanea la carpeta de entrada, verifica, exporta y respalda. |
| `invoicebot run --watch` | Repite el ciclo cada `INVOICEBOT_RUN_INTERVAL_MINUTES` hasta detenerlo. |
| `invoicebot backup` | Copia verificada de la base de datos (`--list` para ver las existentes). |
| `invoicebot serve --open` | Abre la **pantalla de revisión** en el navegador. |
| `invoicebot show <factura>` | Datos, incidencias e historial de una factura. |
| `invoicebot approve <factura>` | Aprueba: confirma los datos y resuelve las advertencias (`--force -n "motivo"` si tiene errores). |
| `invoicebot correct <factura> campo=valor…` | Corrige datos y revalida (`total=118.00 issue_date=15/10/2026`). |
| `invoicebot reject <factura> -r "motivo"` | Rechaza: deja de exportarse y de aparecer en la bandeja. |
| `invoicebot reopen <factura>` · `resolve <incidencia>` | Devuelve a pendiente · da por resuelta una incidencia concreta. |
| `invoicebot doctor` | Comprueba Node.js, SQLite, lectura de PDF, OCR y conexión con la IA. |
| `invoicebot config` | Muestra la configuración efectiva sin revelar secretos. |

Opciones globales: `--provider <anthropic|ollama|mock>`, `--data-dir <ruta>`, `--concurrency <n>`, `--json` (salida para automatización) y `--verbose`.

### Exportación

```bash
invoicebot export --month 2026-10 --xlsx
invoicebot export --month 2026-10 --supplier 20100047218 --currency PEN --bom
```

Por defecto se exportan las facturas **válidas y con advertencias**, y se excluyen las repeticiones de un mismo comprobante. Usa `--include-errors`, `--include-duplicates` o `--status valid` para cambiarlo.

- **Excel (`--xlsx`)**: hoja *Facturas* con importes numéricos (se pueden sumar), fechas reales y RUC/serie/número como texto (conservan los ceros), y hoja *Resumen* con totales por moneda y estado. Formato por defecto configurable con `INVOICEBOT_EXPORT_FORMAT`.
- **CSV**: usa `--bom --delimiter ";"` si lo vas a abrir en Excel con configuración regional en español.

Las columnas, comunes a ambos formatos, están documentadas en [docs/csv-format.md](docs/csv-format.md).

### Revisión humana

El bot no decide solo: lo que tiene errores o advertencias queda **por revisar**. La pantalla de revisión muestra la bandeja, los datos editables, las incidencias y el documento original lado a lado:

```bash
invoicebot serve --open
```

- **Aprobar** confirma la factura y resuelve sus advertencias. Si tiene errores, solo se puede aprobar con una nota que lo justifique.
- **Corregir** cualquier dato la revalida al instante con las mismas reglas (y recalcula duplicados; si cambia RUC, serie, número, fecha o total, se vuelve a consultar en SUNAT).
- **Rechazar** exige un motivo: la factura sale de la bandeja, de los duplicados y de las exportaciones. **Reabrir** la devuelve a pendiente.
- Cada acción queda en la tabla `review_actions` con quién la hizo, cuándo y el valor anterior y nuevo; `invoicebot show <factura>` y la pantalla muestran ese historial. Quién revisa es el usuario de Windows, o `--user` / `INVOICEBOT_USER`.
- Las exportaciones incluyen la columna `review_status` (pendiente, aprobada).

La pantalla escucha solo en `127.0.0.1` (no es accesible desde la red), comprueba la cabecera `Host` y exige un token aleatorio por sesión en cada llamada, con una política de seguridad de contenido estricta. Puerto: `INVOICEBOT_SERVE_PORT` (4317).

### Ejecución desatendida (RPA)

`invoicebot run` hace un ciclo completo sin intervención:

1. Reintenta los documentos que fallaron antes por errores temporales (dentro de `INVOICEBOT_MAX_ATTEMPTS`).
2. Escanea `INVOICEBOT_INBOX_DIR` (con subcarpetas). Puede ser una carpeta de **OneDrive o SharePoint sincronizada**, a la que los usuarios arrastran los comprobantes.
3. Verifica en SUNAT si `INVOICEBOT_SUNAT_VERIFY_ON_SCAN=true`.
4. Exporta el mes actual y el anterior si `INVOICEBOT_RUN_EXPORT=xlsx` o `csv` (las facturas de fin de mes llegan en los primeros días del siguiente). Con `INVOICEBOT_EXPORT_DIR` en una carpeta compartida, el equipo ve el Excel actualizado.
5. Hace una copia de seguridad verificada si la última tiene más de `INVOICEBOT_BACKUP_INTERVAL_HOURS` (24 por defecto) y conserva las `INVOICEBOT_BACKUP_RETENTION` más recientes (14).

Cada ciclo queda en la tabla `bot_runs` (`invoicebot status` muestra las últimas) y, con `INVOICEBOT_LOG_DIR`, en un log diario en JSON que se borra tras `INVOICEBOT_LOG_RETENTION_DAYS` (30). Un candado en `data/invoicebot.lock` impide dos ciclos a la vez: si la tarea programada coincide con un ciclo largo, el nuevo se omite con código 0. Un candado de un proceso que ya no existe se recupera solo.

Para programarlo en Windows cada 15 minutos (con el usuario actual; usa `-WhatIf` para simular y `-Unregister` para quitarlo):

```bash
powershell -ExecutionPolicy Bypass -File scripts/windows/register-scheduled-task.ps1 -IntervalMinutes 15
```

Si prefieres un proceso que quede corriendo, usa `invoicebot run --watch`.

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
        1.º XML UBL ─────→ leer datos (sin IA) ────────────┐   CDR → omitir
        2.º PDF → ¿hay un XML extraído con su mismo nombre? → sí: vincular (sin IA)
                                         ↓ no                  │
            texto del PDF → ¿suficiente? → no: needs_ocr       │
                                         ↓ sí                  │
            LLM (JSON con esquema) ────────────────────────────┤
                                                               ↓
                normalizar → validar → duplicados de negocio → SQLite (transacción) → resumen
```

- **XML primero**: los comprobantes electrónicos traen un XML firmado (UBL 2.1) con los datos exactos. InvoiceBot lo lee sin IA —gratis y sin errores de interpretación— y reparte los importes según el catálogo de tributos de SUNAT (gravadas, exoneradas, inafectas, exportación, ISC, ICBPER). El PDF con el mismo nombre en la misma carpeta se vincula al XML y no se envía a la IA. La constancia de recepción (CDR, `R-*.xml`) se reconoce y se omite. La IA queda para lo que no tiene XML: facturas físicas, tickets y documentos del extranjero. La firma digital del XML todavía no se verifica.
- **Proveedores del extranjero**: facturas de servicios digitales (AWS, Google, Microsoft, Cloudflare…) se registran como tipo 91 (comprobante de no domiciliado) con el identificador fiscal y el país del proveedor, sin exigir RUC, serie SUNAT ni IGV, y con una nota para revisar con el contador el IGV por utilización de servicios y la retención del impuesto a la renta. No se consultan en SUNAT. Probado con 13 facturas reales de Cloudflare: número, fecha, moneda, subtotal, total y emisor correctos en todas.
- **La IA propone, el código decide**: si el modelo dice que un documento no es un comprobante pero extrajo tipo, número, emisor y total, se procesa igual con una advertencia para que una persona lo confirme (caso real: una boleta electrónica bien leída pero marcada como "no comprobante").
- **Idempotencia**: cada archivo se identifica por su SHA-256. Ejecutar dos veces el mismo comando no duplica registros ni repite llamadas a la IA.
- **Concurrencia limitada**: `INVOICEBOT_CONCURRENCY` documentos a la vez (2 por defecto).
- **Reintentos controlados**: un JSON inválido se repite una vez en el momento; los errores de red se reintentan con `retry --failed`; un rechazo del modelo, un documento que no es comprobante o un PDF dañado no se reintentan salvo con `--force`.
- **Trazabilidad**: cada intento queda en `extraction_runs` con proveedor (`ubl-xml` para los XML), modelo que respondió, versión del prompt o del lector, tokens y respuesta cruda; cada factura guarda su origen (`xml` o `ai`) y la versión de las reglas de validación.
- **Actualizaciones seguras**: antes de aplicar una migración a una base de datos con datos, InvoiceBot guarda una copia completa en `data/backups/`.

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
| Comprobante del exterior (tipo 91) con IGV o con RUC peruano | advertencia |
| El modelo dudó si era un comprobante, pero los datos están completos | advertencia |

El IGV no se calcula sobre todo el subtotal cuando hay operaciones exoneradas o inafectas: se usa el desglose tributario del comprobante. La aritmética se hace con enteros en céntimos y una tolerancia explícita (`INVOICEBOT_AMOUNT_TOLERANCE`, 0.02 por defecto).

> Validar matemáticamente un RUC **no** confirma que la empresa exista, esté activa ni que el comprobante sea auténtico. Para eso está `invoicebot verify-sunat`.

### Verificación en SUNAT

Es **opcional y está desactivada por defecto**: InvoiceBot no contacta a SUNAT hasta que lo actives en la configuración. Cuando está activa, usa el servicio oficial de SUNAT **Consulta Integrada de Comprobante de Pago** para confirmar cada comprobante registrado. Una sola consulta devuelve:

| Dato | Resultado en InvoiceBot |
| --- | --- |
| Estado del comprobante: ACEPTADO, AUTORIZADO | Sin incidencias |
| ANULADO (comunicado de baja) o NO AUTORIZADO (físico sin autorización de imprenta) | error |
| NO EXISTE (no informado a SUNAT) | error en facturas; advertencia en boletas, que llegan en el resumen diario y pueden tardar días |
| Estado del RUC emisor distinto de ACTIVO (baja, suspensión…) | advertencia |
| Domicilio del emisor NO HABIDO o NO HALLADO | advertencia: revisa con tu contador el crédito fiscal |
| Observaciones de SUNAT | información |

```bash
invoicebot verify-sunat                    # comprobantes sin verificar y los aún no informados
invoicebot verify-sunat --month 2026-10
invoicebot verify-sunat --force --limit 50 # vuelve a consultar también los ya verificados
invoicebot scan ./facturas --verify-sunat  # escanea y verifica al terminar
```

| Variable | Valor por defecto | Uso |
| --- | --- | --- |
| `INVOICEBOT_SUNAT_ENABLED` | `false` | Interruptor general. Con `false`, `verify-sunat` y `scan --verify-sunat` terminan con código 2 y `doctor` la muestra como desactivada. |
| `INVOICEBOT_SUNAT_VERIFY_ON_SCAN` | `false` | `true`: cada `scan` verifica al terminar las facturas pendientes. Se puede anular en una ejecución con `scan --no-verify-sunat`. Requiere `ENABLED=true`. |
| `INVOICEBOT_SUNAT_RUC`, `_CLIENT_ID`, `_CLIENT_SECRET` | — | Credenciales de API SUNAT (ver abajo). El secreto nunca se muestra ni se registra en logs. |
| `INVOICEBOT_SUNAT_TIMEOUT_MS` | `20000` | Espera máxima por consulta (1 000 – 120 000). |
| `INVOICEBOT_SUNAT_DELAY_MS` | `0` | Pausa entre consultas consecutivas, para lotes grandes. |
| `INVOICEBOT_SUNAT_AUTH_URL`, `_API_URL` | URLs oficiales | Solo para un proxy corporativo o un entorno de pruebas. |

Si la verificación automática falla después de escanear (red caída, credenciales vencidas), lo escaneado queda guardado igualmente y el comando termina con código 3.

Las incidencias de SUNAT se suman a las de validación y recalculan el estado de la factura: un comprobante anulado deja de exportarse por defecto. El resultado de cada consulta queda en la tabla `sunat_checks` y el último estado en la columna `sunat_status` del CSV. Las consultas son secuenciales; un fallo de red se registra y se reintenta en la siguiente ejecución, y unas credenciales inválidas detienen el lote.

**Credenciales**: entra en SUNAT Operaciones en Línea con la Clave SOL de tu empresa → *Credenciales de API SUNAT*, registra una aplicación y copia su `client_id` y `client_secret` en `.env` (`INVOICEBOT_SUNAT_RUC`, `INVOICEBOT_SUNAT_CLIENT_ID`, `INVOICEBOT_SUNAT_CLIENT_SECRET`) junto con `INVOICEBOT_SUNAT_ENABLED=true`. Comprueba la conexión con `invoicebot doctor`. Referencia: [Manual de Consulta Integrada de Comprobante de Pago por Servicio Web](https://cpe.sunat.gob.pe/sites/default/files/inline-files/Manual-de-Consulta-Integrada-de-Comprobante-de-Pago-por-ServicioWEB_v2_0.pdf).

## Seguridad y privacidad

- `.env`, `data/`, `exports/`, logs y archivos PDF/PNG/JPG están en `.gitignore`: no subas facturas reales.
- Las claves de API se leen solo de variables de entorno y `invoicebot config` nunca las muestra.
- Los logs van a stderr y no incluyen el texto de las facturas.
- El contenido de cada documento se envía al modelo delimitado como datos, con instrucciones explícitas de ignorar cualquier orden que contenga.
- Con `INVOICEBOT_AI_PROVIDER=ollama`, ningún documento sale del equipo.
- `verify-sunat` envía a SUNAT solo los identificadores de cada comprobante (RUC emisor, tipo, serie, número, fecha y total), nunca el documento.

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
