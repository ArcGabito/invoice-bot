# InvoiceBot

![Wallpaper](https://media.gabito.dev/imagenes/1793201727477-invoice-bot-wallpaper.png)

**Bot RPA de comprobantes de pago peruanos** en Python, con API REST (FastAPI) y SQL Server.

InvoiceBot recibe facturas, boletas y notas desde una carpeta, el correo o su API; lee el XML del comprobante electrónico cuando existe (sin IA) y, si no, el PDF, la foto o el escaneo con OCR e IA (Claude); **comprueba con código determinista** que los datos sean coherentes (RUC, IGV, totales, detracciones, tipo de cambio), detecta duplicados, verifica en SUNAT y lo guarda todo en SQL Server con trazabilidad. Lo dudoso queda en una bandeja de revisión web; lo correcto sale al Excel contable.

> El XML o el LLM proponen los datos; el código comprueba su consistencia; una persona resuelve los casos dudosos.

**El problema que resuelve**: en un área contable, registrar cada comprobante de proveedor a mano (descargarlo del correo, teclear RUC, serie, importes, validar en SUNAT, calcular la detracción, convertir dólares) toma minutos por documento y es donde se cuelan los errores que luego observa SUNAT: facturas anuladas, RUC no habidos, IGV mal calculado, duplicados pagados dos veces.

## Estado: v1.0.0 (Python)

La versión 1.0 es la migración completa a Python de la 0.6 (Node.js, etiqueta `v0.6.0-node`). Mismas reglas, mismos comandos, mismas variables de entorno y **mismo esquema de base de datos**: una instalación existente se actualiza sin perder datos ni contraseñas de usuarios.

| Funcionalidad | Estado |
| --- | --- |
| Escaneo de carpetas (XML, PDF, PNG, JPG), SHA-256 e idempotencia | ✅ |
| Comprobantes electrónicos SUNAT en XML UBL 2.1, sin IA; PDF vinculado a su XML; CDR omitido | ✅ |
| Extracción con IA: Claude (salida estructurada + respaldo del servidor), Ollama local o simulado | ✅ |
| OCR de escaneos y fotos: Tesseract local o visión de Claude | ✅ |
| Validación: RUC (módulo 11), fechas, serie, aritmética tributaria, IGV 18 % y 10,5 % | ✅ |
| Proveedores del exterior (tipo 91), anticipos, detracciones (SPOT) y tipo de cambio de SUNAT | ✅ |
| Duplicados exactos y por clave de negocio, con candado en SQL Server | ✅ |
| Verificación en SUNAT (comprobante aceptado/anulado, RUC activo/habido) | ✅ |
| Lectura de correo: IMAP, Gmail / Google Workspace y Microsoft 365 (también modo empresa) | ✅ |
| Revisión humana: pantalla web y CLI para aprobar, corregir, rechazar, con auditoría | ✅ |
| Exportación a CSV y Excel con totales en soles y neto a pagar | ✅ |
| **API REST (FastAPI)** con OpenAPI, claves de API y subida de documentos | ✅ nuevo |
| **Azure Container Apps + Jobs** (Bicep), **VPS** (Docker Compose + Caddy) y **Windows** (tareas programadas) | ✅ nuevo |
| **Cadena de conexión de SSMS**, seguridad integrada de Windows e identidad administrada de Azure | ✅ nuevo |
| Solución de **Visual Studio** (`invoice-bot.slnx` + proyecto Python) | ✅ |

## Arquitectura

```text
           correo (IMAP/Gmail/Graph) ─┐
 carpeta / OneDrive / Azure Files ────┼──► ciclo RPA (invoicebot run) ──► SQL Server / Azure SQL
 otros sistemas ── POST /api/v1/documents ┘        │                          ▲
                                                   ▼                          │
               XML UBL ─► lector sin IA ─┐   normalizar → validar →           │
               PDF/foto ─► texto/OCR ─► IA ┘   duplicados → SUNAT → guardar ───┘
                                                                              │
             pantalla de revisión · API REST · Excel/CSV · Power BI (v_invoices)
```

```text
src/invoicebot/
  domain/          reglas puras: normalización, validación, detracciones, SUNAT, duplicados
  application/     casos de uso: procesar, escanear, reintentar, revisar, exportar, ciclo RPA
  infrastructure/  SQL Server, UBL, PDF (PDFium), OCR, Claude/Ollama, SUNAT, correo, Excel
  api/             FastAPI: REST, autenticación, pantalla de revisión
  cli.py           Typer: los mismos comandos de la versión 0.x
```

## Instalación rápida (desarrollo)

Requisitos: Python 3.12+, [ODBC Driver 18 for SQL Server](https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server) y un SQL Server (Express sirve) o Azure SQL.

```bash
git clone https://github.com/ArcGabito/invoice-bot.git && cd invoice-bot
python -m venv .venv
.venv\Scripts\activate          # Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"
copy .env.example .env          # Linux/macOS: cp
```

En `.env`, la conexión puede ser la cadena **tal como la copias de SSMS**:

```dotenv
INVOICEBOT_DB_CONNECTION_STRING=Data Source=TUF-F16\GABODB;Integrated Security=True;Encrypt=True;TrustServerCertificate=True
INVOICEBOT_DB_NAME=invoicebot
INVOICEBOT_AI_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
```

```bash
invoicebot db init                       # crea la base y aplica las migraciones
invoicebot user add admin --name "Admin" # usuario de la pantalla de revisión
invoicebot doctor                        # comprueba SQL Server, PDF, OCR, IA, SUNAT y correo
invoicebot scan ./facturas
invoicebot serve --open                  # API + pantalla de revisión en http://127.0.0.1:4317
```

Proveedores de IA: `anthropic` (Claude, `claude-opus-5-5` por defecto, con salida estructurada validada y respaldo del servidor si el modelo rechaza un documento), `ollama` (local: los documentos no salen del equipo) o `mock` (expresiones regulares, sin coste, para probar el flujo).

## Despliegue

| Dónde | Cómo | Guía |
| --- | --- | --- |
| **Azure Container Apps** | Container App (API) + Container Apps Job programado (ciclo RPA) + Azure SQL serverless con identidad administrada + Azure Files | [docs/despliegue-azure.md](docs/despliegue-azure.md) |
| **VPS Linux** | Docker Compose: `api`, `worker`, SQL Server opcional y Caddy (HTTPS) | [docs/despliegue-vps.md](docs/despliegue-vps.md) |
| **VM / servidor Windows** | Seguridad integrada, tareas programadas, cuenta de servicio gMSA | [docs/instalacion-windows.md](docs/instalacion-windows.md) |

La imagen (`Dockerfile`) incluye ODBC 18 y Tesseract con español, corre sin privilegios y sirve tanto para la API (`serve`) como para el ciclo (`run`).

## CLI

| Comando | Función |
| --- | --- |
| `invoicebot scan ./facturas [--recursive] [--dry-run] [--verify-sunat]` | Procesa los documentos de una carpeta (o un archivo). |
| `invoicebot run [--watch] [--trigger scheduled]` | Ciclo desatendido: correo, tipo de cambio, reintentos, escaneo, SUNAT, exportación y copia. |
| `invoicebot serve [--host] [--port] [--workers] [--open]` | API REST y pantalla de revisión. |
| `invoicebot status` · `review` · `duplicates` · `show <id>` | Consultas. |
| `invoicebot approve <id>` · `correct <id> campo=valor…` · `reject <id> -r motivo` · `reopen <id>` · `resolve <incidencia>` | Revisión con auditoría. |
| `invoicebot export --month 2026-10 --xlsx` | Exporta a Excel o CSV ([formato](docs/csv-format.md)). |
| `invoicebot retry --failed` · `retry --pending` | Reintentos y recuperación de ejecuciones interrumpidas. |
| `invoicebot verify-sunat [--month] [--force]` | Consulta integrada de SUNAT. |
| `invoicebot fx set 15/10/2026 3.750` · `fx fetch` · `fx import tc.csv` · `fx list` | Tipo de cambio. |
| `invoicebot mail check` · `mail fetch` · `mail list` · `mail login` | Correo ([modo empresa](docs/correo-empresa.md)). |
| `invoicebot user add/list/reset-password/disable/enable` | Usuarios de la pantalla de revisión. |
| `invoicebot apikey create <sistema> --scope write` · `list` · `revoke` | Claves de la API para otros sistemas. |
| `invoicebot db init` · `db migrate` · `db status` · `backup [--list]` | Base de datos. |
| `invoicebot doctor` · `config` | Diagnóstico y configuración efectiva (sin secretos). |

Opciones globales: `--provider`, `--data-dir`, `--concurrency`, `--json` (salida para automatización), `--verbose`, `--user`.

Códigos de salida: `0` completado · `1` error general · `2` argumentos o configuración · `3` ejecución parcial (documentos fallidos o facturas con errores; configurable con `INVOICEBOT_RUN_PARTIAL_EXIT_CODE`).

## API REST

Documentación interactiva en `/docs` (OpenAPI). Autenticación:

- **Personas**: `POST /api/v1/auth/login` → cookie de sesión `HttpOnly`, `SameSite=Strict`, `Secure` con HTTPS; cada acción lleva la cabecera `X-CSRF-Token`. Bloqueo de 15 minutos tras 5 intentos fallidos.
- **Sistemas**: `Authorization: Bearer ibk_…` o `X-API-Key: ibk_…` (`invoicebot apikey create`), con alcance `read` o `write`.

| Método y ruta | Uso |
| --- | --- |
| `POST /api/v1/documents` (multipart `files`) | Recibe comprobantes de otro sistema y los procesa al instante (el XML y su PDF juntos se vinculan). |
| `GET /api/v1/invoices?month=&validation_status=&q=` | Listado paginado. |
| `GET /api/v1/invoices/{id}` · `/document` | Detalle con incidencias, historial y correo de origen · archivo original. |
| `POST /api/v1/invoices/{id}/approve` · `correct` · `reject` · `reopen` · `POST /api/v1/issues/{id}/resolve` | Revisión. |
| `GET /api/v1/queue?view=pending` | Bandeja de revisión. |
| `GET /api/v1/exports?month=&format=xlsx` | Excel o CSV. |
| `GET/POST /api/v1/fx` | Tipos de cambio. |
| `POST /api/v1/sunat/verify` | Verificación en SUNAT. |
| `GET/POST /api/v1/runs` | Historial y lanzamiento de ciclos (el candado de SQL Server evita dos a la vez). |
| `GET /healthz` · `GET /readyz` | Sondas de vida y de disponibilidad (base de datos y esquema). |

```bash
curl -H "Authorization: Bearer $INVOICEBOT_API_KEY" \
  -F files=@F001-00001234.xml -F files=@F001-00001234.pdf \
  https://facturas.empresa.pe/api/v1/documents
```

## Cómo funciona

```text
scan → descubrir archivos → SHA-256 → ¿ya procesado? → sí: omitir
                                         ↓ no
        1.º XML UBL ─────→ leer datos (sin IA) ────────────┐   CDR → omitir
        2.º PDF → ¿hay un XML extraído con su mismo nombre? → sí: vincular (sin IA)
                                         ↓ no                  │
            texto del PDF → ¿suficiente? → no: OCR (Tesseract) │   sin texto legible → needs_ocr
                                         ↓ sí                  │
            LLM (JSON con esquema) ────────────────────────────┤
                                                               ↓
                normalizar → validar → duplicados de negocio → SQL Server (transacción) → resumen
```

- **XML primero**: los comprobantes electrónicos traen un XML UBL 2.1 con los datos exactos. Se lee sin IA (gratis y sin errores de interpretación) y los importes se reparten según el catálogo de tributos de SUNAT. La firma digital no se verifica.
- **La IA propone, el código decide**: si el modelo duda de que sea un comprobante pero extrajo tipo, número, emisor y total, se procesa con una advertencia para que una persona lo confirme.
- **Idempotencia**: cada archivo se identifica por su SHA-256; repetir un escaneo no duplica registros ni llamadas a la IA.
- **Concurrencia controlada**: `INVOICEBOT_CONCURRENCY` documentos a la vez; la búsqueda de duplicados y el guardado van en una transacción con la clave de negocio reservada (`sp_getapplock`).
- **Trazabilidad**: cada intento queda en `extraction_runs` (proveedor, modelo que respondió, versión del prompt o del lector, tokens, respuesta cruda); cada acción de revisión en `review_actions`; cada ciclo en `bot_runs`.
- **Varios procesos a la vez**: el bot, la API y otras réplicas comparten la base sin bloquearse (`READ_COMMITTED_SNAPSHOT`); un candado de sesión en SQL Server impide dos ciclos simultáneos aunque corran en contenedores distintos.
- **Actualizaciones seguras**: antes de aplicar una migración a una base con datos se hace una copia completa (salvo en Azure SQL, que tiene copias automáticas).

### Reglas de validación

| Regla | Severidad |
| --- | --- |
| Campos obligatorios: RUC emisor, tipo, serie, número, fecha, moneda, total | error |
| RUC del emisor: 11 dígitos, prefijo 10/15/16/17/20 y dígito de control | error |
| Fecha de emisión inexistente | error |
| Total ≠ base + IGV + ISC + ICBPER + otros (con descuentos y anticipos), fuera de la tolerancia | error |
| Importe ilegible o negativo; moneda que no es ISO 4217 | error |
| IGV que no corresponde al 18 % ni al 10,5 % de la base gravada | advertencia |
| Fecha futura, serie no SUNAT o de otro tipo de comprobante, RUC del cliente inválido | advertencia |
| Posible duplicado; comprobante del exterior con IGV o con RUC peruano | advertencia |
| Sin tipo de cambio para la fecha (`FX_RATE_MISSING`); detracción incompleta o mal calculada | advertencia |
| SUNAT: anulado, no autorizado o no informado | error |
| SUNAT: RUC no activo, domicilio no habido | advertencia |

> Validar matemáticamente un RUC **no** confirma que la empresa exista ni que el comprobante sea auténtico. Para eso está `invoicebot verify-sunat`.

## Seguridad y privacidad

- `.env`, datos, exportaciones, logs y archivos PDF/PNG/JPG están en `.gitignore`: no subas facturas reales.
- Los secretos se leen solo del entorno; `invoicebot config` nunca los muestra. En Azure, la base usa identidad administrada (sin contraseñas).
- La conexión con SQL Server va cifrada; `INVOICEBOT_DB_TRUST_CERT=true` solo en una red de confianza.
- El XML se analiza con `defusedxml` (sin entidades externas); los adjuntos ZIP tienen límites de tamaño y de archivos; los nombres de archivo se sanean.
- El contenido de cada documento se envía al modelo delimitado como datos, con instrucciones de ignorar cualquier orden que contenga.
- Con `ollama` y Tesseract, ningún documento sale del equipo.
- Pantalla de revisión: contraseñas con scrypt, sesiones con hash en la base, CSRF en cada acción, CSP estricta y el XML original servido como texto plano.

## Desarrollo

```bash
pytest tests/unit        # sin SQL Server
pytest                   # unitarias + integración contra un SQL Server real
ruff check src tests && ruff format --check src tests && mypy
```

Las pruebas de integración crean y borran su propia base `invoicebot_test_<aleatorio>`. La conexión se lee de `INVOICEBOT_TEST_DB_CONNECTION_STRING` (también en formato SSMS) o de `INVOICEBOT_TEST_DB_SERVER`, `_PORT`, `_USER`, `_PASSWORD`, `_TRUST_CERT`, del entorno o de `.env.test`. En GitHub Actions corren contra un contenedor de SQL Server 2022.

Visual Studio: abre `invoice-bot.slnx` (carga de trabajo *Desarrollo de Python*); F5 inicia `invoicebot serve` con el entorno `.venv` y el *Explorador de pruebas* usa pytest.

## Licencia

[MIT](LICENSE.md)
