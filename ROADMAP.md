# Plan por fases: InvoiceBot

> **Objetivo**: que InvoiceBot automatice de punta a punta el proceso de **cuentas por pagar** de una empresa peruana, desde que llega el comprobante del proveedor hasta que queda registrado, aprobado, pagado y cuadrado con SUNAT. Debe desplegarse con la misma imagen en un **VPS** (Docker Compose) o en **Azure Container Apps**.
>
> Stack: **Python 3.13 + uv · FastAPI · SQL Server / Azure SQL · Redis · colas (Dramatiq) · Docker**.

## Punto de partida (v1.0.0)

**Ya funciona**: lectura de XML UBL sin IA, PDF/OCR/IA (Claude, Ollama), validación tributaria determinista, duplicados con `sp_getapplock`, verificación en SUNAT, correo (IMAP, Gmail, Microsoft 365), revisión humana web y CLI, exportación a Excel/CSV, API REST con claves, Bicep para Azure, Compose para VPS y tareas programadas en Windows.

**Falta para una operación empresarial**:

- El ciclo `invoicebot run --watch` es **un solo proceso** que hace todo en serie: correo, escaneo, SUNAT y exportación, con un `ThreadPoolExecutor` dentro. No escala horizontalmente y un documento lento frena a los demás.
- `POST /api/v1/documents` procesa **dentro de la petición HTTP** (OCR e IA bloquean la respuesta).
- Las dependencias se instalan con `pip` y no hay archivo de bloqueo, así que las compilaciones no son reproducibles.
- Una sola empresa receptora, sin roles por función, sin flujo de aprobación, sin SIRE y sin integración con el ERP o con tesorería.

## Arquitectura objetivo

```text
 proveedores ─ correo / portal / carpeta / ERP
                     │
          ┌──────────▼──────────┐   encola    ┌──────────────── Redis ────────────────┐
 Caddy ─► │ api (FastAPI) x N   │ ──────────► │ colas · caché · candados · rate limit │
 HTTPS    │ sin estado          │             └───┬───────────────────────▲───────────┘
          └──────────┬──────────┘                 │ consume               │ encola (cron)
                     │ lee/escribe                ▼                       │
                     │                 ┌────────────────────┐   ┌─────────┴──────────┐
                     └───────────────► │ SQL Server         │◄──│ worker(s) Dramatiq │
                                       │ fuente de verdad   │   │ scheduler (1 líder)│
                                       │ + outbox           │   └────────────────────┘
                                       └────────────────────┘
```

| Proceso | Comando | VPS (Docker Compose) | Azure Container Apps |
| --- | --- | --- | --- |
| `api` | `invoicebot serve` | servicio `api` tras Caddy | Container App con ingress, escala por HTTP (mín. 1) |
| `worker` | `invoicebot worker --queues ingest,default,sunat` | servicio `worker`, `--scale worker=N` | Container App sin ingress, escala de 0 a N con KEDA (`redis`) según la longitud de cola |
| `worker-extract` | `invoicebot worker --queues extract` | servicio aparte con más CPU/RAM (OCR) | Container App con perfil de más CPU, de 0 a N con KEDA |
| `scheduler` | `invoicebot scheduler` | 1 réplica, candado de líder en Redis | Container Apps Job programado: `invoicebot schedule tick` cada 5 min |
| `migrate` | `invoicebot db migrate` | `docker compose run --rm api db migrate` | Container Apps Job manual antes de activar cada revisión |
| Redis | — | `redis` con AOF y `noeviction` | Azure Managed Redis (o Redis en VPS privado) |
| SQL Server | — | perfil `sql` (Express) o servidor externo | Azure SQL serverless con identidad administrada |

### Decisiones técnicas

| Tema | Decisión | Motivo |
| --- | --- | --- |
| Dependencias | **uv** con `uv.lock`, `.python-version` y `[dependency-groups] dev` | Compilaciones reproducibles e instalación 10 a 100 veces más rápida que pip |
| Colas | **Dramatiq + Redis** | Trabaja con hilos, igual que el código actual (pyodbc síncrono). Trae reintentos con backoff, cola de mensajes muertos (DLQ), límites de tiempo, rate limiters sobre Redis y métricas Prometheus. Celery es más pesado, y arq o Taskiq obligarían a reescribir el acceso a datos en asíncrono |
| Fuente de verdad | **SQL Server** | Los trabajos se registran en una tabla `jobs`; Redis guarda los mensajes y el progreso en vivo, nunca el único estado de negocio |
| Consistencia | **Outbox transaccional** | El cambio de negocio y el evento se guardan en la misma transacción, y un relay los publica en Redis. Si Redis cae, no se pierde nada |
| Idempotencia | SHA-256 por archivo (ya existe) + cabecera `Idempotency-Key` en la API (Redis, 24 h) | Reintentar no duplica comprobantes ni llamadas a la IA |
| Modo sin Redis | `INVOICEBOT_QUEUE_BACKEND=inline` | Ejecuta los actores en el mismo proceso. Sirve para pruebas, desarrollo e instalaciones pequeñas en Windows |
| Redis | `appendonly yes`, `maxmemory-policy noeviction`, ACL con contraseña, sin puerto público | Con desalojo de claves se perderían trabajos de la cola |

### Colas de InvoiceBot

| Cola | Actores | Límites |
| --- | --- | --- |
| `ingest` | `mail.fetch(mailbox_id)`, `files.discover(source_id)`, `portal.upload(upload_id)` | Uno por buzón a la vez (candado en Redis) |
| `extract` | `documents.process(document_id)`: XML, PDF, OCR, IA, validación y guardado | Concurrencia por CPU; rate limit de tokens por minuto de Claude |
| `sunat` | `sunat.verify(invoice_id)`, `sunat.ruc_status(ruc)`, `fx.fetch(date)`, `sire.sync(company, period)` | Ventana deslizante en Redis para no saturar SUNAT; caché de estado de RUC (24 h) y tipo de cambio |
| `default` | `exports.monthly(company, month)`, `approvals.remind()`, `db.backup()`, `webhooks.deliver(event_id)`, `outbox.relay()` | — |

Cada actor es **idempotente**: recibe un identificador, no el documento, y vuelve a leer el estado desde SQL Server.

---

## Fase 0: Plataforma (uv, Redis, colas y despliegue) ✅

**Objetivo**: mismo comportamiento funcional de la v1.0, pero sobre colas, listo para escalar y con despliegue reproducible.

- [x] **uv**: `uv.lock` versionado, `.python-version` (3.13), grupo `dev` (PEP 735) y `uv run` en la documentación. `pip install -e .` sigue funcionando.
- [x] **Dockerfile** multietapa con uv (`uv sync --locked --no-dev` con caché de BuildKit). La imagen final mantiene ODBC 18 y Tesseract, sin uv ni compiladores, con usuario 10001.
- [x] **Redis + Dramatiq**: `infrastructure/queue/` (broker, actores por cola, worker con apagado ordenado por SIGTERM, límites de llamadas) y las tareas de `application/tasks.py`. Comandos `invoicebot worker`, `scheduler`, `schedule tick|list|set|enable|disable`, `enqueue`, `tasks` y `jobs list|show|retry|cancel|run`. `run` se mantiene con la cola `inline`.
- [x] **Tablas `jobs` y `schedules`** (migración 006). Los trabajos se deduplican mientras están activos y admiten claves de idempotencia.
- [x] **API asíncrona**: `POST /api/v1/documents` responde `202` con el trabajo (con Redis), `GET /api/v1/jobs/{id}`, progreso por SSE y `Idempotency-Key`.
- [x] **Candados**: el ciclo y el programador usan `sp_getapplock` (SQL Server). *Cambio respecto del plan*: no se movieron a Redis, porque así funcionan igual con y sin Redis y se liberan solos si el proceso muere.
- [x] **Esperas y reintentos**: *cambio respecto del plan*: no usan la cola de espera de Dramatiq. Quedan en SQL Server (`run_after`) y los publica el programador, así Redis solo tiene trabajos listos y KEDA puede bajar los workers a cero.
- [x] **Límites de llamadas** a la IA y a SUNAT compartidos entre workers (ventana deslizante en Redis).
- [ ] **Caché** del estado de RUC y padrones de SUNAT en Redis: queda para la Fase 3 (SIRE y reverificación).
- [x] **Salud y métricas**: `/readyz` comprueba SQL Server, esquema y la cola. `/metrics` publica trabajos por tarea y estado, la espera del más antiguo y los mensajes listos y muertos por cola.
- [x] **Docker Compose**: `api`, `worker`, `extractor`, `scheduler`, `redis` (AOF, `noeviction`, contraseña) y `migrate`, más los perfiles `sql` y `proxy`.
- [x] **Azure (Bicep)**: `mode=queue` con Azure Managed Redis, Container Apps `worker` y `extractor` con reglas KEDA `redis`, Job `scheduler` y Job `migrate`. `mode=cycle` mantiene el Job clásico. El workflow ejecuta las migraciones antes de dar por terminado el despliegue.
- [x] **CI/CD**: `astral-sh/setup-uv`, `uv lock --check` y pruebas con SQL Server **y Redis** como servicios (incluido un worker Dramatiq real).

**Hecho cuando**: las pruebas pasan con `inline` y con `redis`, y un lote de documentos se procesa en un worker real sin pérdidas ni duplicados (`tests/integration/test_jobs.py`).

## Fase 1: Multiempresa, seguridad y auditoría ✅

**Objetivo**: que un estudio contable o un grupo de empresas use una sola instalación con permisos por función.

- [x] Tabla `companies` (RUC adquirente, razón social, agente de retención, buen contribuyente) y `company_id` en `invoices`, asignado automáticamente por el RUC del adquirente. Si hay empresas y el adquirente no es ninguna: advertencia `CUSTOMER_NOT_A_COMPANY`.
- [x] **Roles**: `admin`, `accountant` (contabilidad), `approver`, `treasury` y `auditor`, con un permiso por endpoint. Cada usuario puede limitarse a ciertas empresas.
- [x] **SSO OIDC** con Microsoft Entra ID y Google Workspace (PKCE, JWKS, nonce, dominios permitidos y alta automática opcional). Los usuarios locales se mantienen.
- [x] **Auditoría**: *cambio respecto del plan*: triggers (`invoice_changes`, `company_changes`) en vez de tablas temporales. Con `READ_COMMITTED_SNAPSHOT` y conexiones reutilizadas del pool, las tablas temporales rechazan modificaciones concurrentes (error 13535; reproducido en las pruebas). Cada cambio registra quién lo hizo: persona, clave de API o trabajo.
- [x] **Row-Level Security** de SQL Server por `company_id` (filtro y bloqueo tras actualizar), fijada por petición con `SESSION_CONTEXT`.
- [x] Claves de API por empresa y con permisos finos; el límite de llamadas por clave en Redis queda para la Fase 6.

**Hecho cuando**: una persona de la empresa A no puede ver por la API un comprobante de la empresa B, y cada cambio de un comprobante aparece en el historial con su autor (`tests/integration/test_access.py`).

## Fase 2: Flujo de cuentas por pagar (match y aprobación) ✅

**Objetivo**: que una factura correcta llegue a "aprobada para pago" sin que nadie la toque, y que solo las excepciones lleguen a una persona.

- [x] **Órdenes de compra y recepciones** (notas de ingreso, guías de remisión, conformidades) por API o CSV. Las guías de document-bot entran por la misma API.
- [x] **Match a 2 y 3 vías** con tolerancias configurables y una excepción legible por cada diferencia. La orden se encuentra por la referencia del XML, por la guía o por proveedor e importe.
- [x] **Matriz de aprobación** por empresa, centro de costo, proveedor y monto en soles, por niveles, con delegaciones por vacaciones.
- [x] Aprobación con un clic desde el correo (enlace firmado de un solo uso, evento `approval.requested` para email-bot) o desde la API y la CLI.
- [x] Recordatorios y **escalamiento por SLA** (`approvals.remind`, programado).
- [x] **Cuenta contable y centro de costo**: de la orden y aprendidos de las correcciones por proveedor. *Pendiente*: la propuesta con IA cuando no hay historial.
- [x] Notas de crédito y débito aplicadas a su comprobante, con el saldo en `v_invoice_balances`.
- [x] *Adelantado de la Fase 4*: eventos con outbox y **webhooks firmados** (HMAC-SHA256, CloudEvents, reintentos).
- [x] El código nuevo usa **SQLAlchemy ORM** (modelos declarativos 2.0), como pidió el equipo.

**Hecho cuando**: una factura que coincide con su orden y su recepción queda autorizada para pago sin intervención humana, y cada excepción tiene su motivo (`tests/integration/test_purchasing.py`).

## Fase 3: Cumplimiento tributario (SUNAT) ✅

**Objetivo**: cerrar el mes de compras cuadrado con SUNAT y sin perder crédito fiscal.

- [x] **SIRE, Registro de Compras Electrónico**: importación de la propuesta descargada (TXT, CSV o ZIP, lectura por encabezados) y cuadre con lo registrado. El reporte en Excel muestra faltantes, sobrantes y diferencias de importe, IGV o fecha.
- [ ] *Pendiente*: la descarga automática con el API SIRE (credenciales de SUNAT y tickets) y el archivo de ajustes. Se harán con el manual vigente del API y una cuenta de pruebas; no se inventaron endpoints.
- [x] **Reverificación programada** en SUNAT del mes anterior antes del cierre (`sunat.reverify`).
- [x] **Detracciones (SPOT)**: pendientes, constancias de depósito, alerta de detracciones sin depositar y **archivo de depósito masivo** (layout aislado para verificarlo con la guía vigente).
- [x] **Retenciones**: cálculo del 3 % con la condición del proveedor (padrones de buenos contribuyentes y agentes). *Pendiente*: emitir el comprobante de retención (requiere un OSE o SEE).
- [x] Alertas de **plazo de anotación** (12 periodos) y de detracciones pendientes (`compliance.alerts`).

**Hecho cuando**: el cuadre del mes contra el SIRE sale en un comando o una llamada a la API y lista cada diferencia con su comprobante (`tests/integration/test_tax.py`).

## Fase 4: Contabilidad, pagos e integración ✅

**Objetivo**: que lo aprobado se contabilice y se pague sin volver a digitarlo.

- [x] **Asiento de provisión** según el PCGE (60/63, 40111, 4212 y el destino 9x/79 por centro de costo), cuadrado también en moneda extranjera. Exportación CSV, Excel y plantilla tipo Concar; API JSON para el ERP (Vortex ERP, Odoo). *Pendiente*: la plantilla de Siscont.
- [x] **Programación de pagos** por vencimiento o condición de pago, con el neto de retención, detracción y notas de crédito. Se publica a **bank-bot** como partida por pagar (`invoice.payment_scheduled`, `GET /api/v1/payables`).
- [x] bank-bot confirma el pago (`POST /api/v1/payments`, idempotente) y la factura pasa a **pagada** (`invoice.paid`).
- [x] **Webhooks firmados** (hechos en la Fase 2) con los eventos `invoice.received`, `invoice.rejected`, `invoice.payment_scheduled` e `invoice.paid`.
- [x] **Avisos a proveedores**: los eventos llevan el correo de origen del comprobante para que email-bot envíe el acuse, el rechazo con motivo y la fecha de pago.

**Hecho cuando**: una factura aprobada aparece como partida por pagar y, al confirmarse su pago, queda pagada sin intervención manual (`tests/integration/test_payables.py`).

## Fase 5: Portal de proveedores, antifraude y analítica ✅

- [x] **Portal de proveedores**: enlace mágico por correo (sin enumerar contactos), subida de XML y PDF, y consulta del estado y la fecha de pago. Los contactos se aprenden del correo de origen.
- [x] **Antifraude**: proveedor nuevo con monto alto, importe fuera del rango histórico y duplicados difusos. Son advertencias que impiden la aprobación automática y publican `invoice.fraud_alert`. El cambio de cuenta bancaria lo cubre bank-bot y el RUC dado de baja, la verificación en SUNAT.
- [x] **Indicadores**: procesamiento sin intervención (STP), tiempos de ciclo, costo de IA por comprobante y comprobantes observados, en la API, la CLI y la vista `v_invoice_lifecycle` para Power BI. *Pendiente*: un panel gráfico propio (la vista alimenta Power BI).

## Fase 6: Producción a escala

- [ ] **Observabilidad**: OpenTelemetry en FastAPI, Dramatiq, httpx y SQL. Se envía a Azure Monitor o, en un VPS, a Grafana (Prometheus, Loki y Tempo). Alertas por cola creciendo, DLQ mayor que 0, edad del mensaje más antiguo y errores de IA o SUNAT.
- [ ] **SLO**: p95 de la API, tiempo desde la recepción hasta el comprobante validado y porcentaje de trabajos fallidos.
- [ ] **Alta disponibilidad y recuperación**: API con 2 o más réplicas, Redis con persistencia y réplica (Azure Managed Redis con alta disponibilidad), Azure SQL con redundancia de zona o SQL Server con copias probadas, y un runbook de recuperación con RPO y RTO definidos.
- [ ] **Despliegues sin cortes**: revisiones de Container Apps con división de tráfico (blue/green) y, en el VPS, actualización gradual con healthchecks. Las migraciones corren siempre como job previo.
- [ ] **Seguridad continua**: Dependabot o Renovate para `uv.lock`, Trivy y SBOM por versión, revisión con OWASP ASVS nivel 2 y secretos solo en Key Vault o Docker secrets.
- [ ] **Pruebas de carga** con k6 o Locust: 10 000 comprobantes por día por empresa como referencia.

---

## Integración con la suite de bots

Cada bot tiene **su propia base de datos** y se despliega por separado. Se comunican por **API REST con claves** y **webhooks firmados** en formato CloudEvents, publicados desde la outbox.

| Evento | Emisor → receptor | Efecto |
| --- | --- | --- |
| `email.attachment.received` (comprobante) | email-bot → **invoice-bot** | Procesa el XML o el PDF recibido |
| `document.classified` (guía de remisión) | document-bot → **invoice-bot** | Recepción para el match a 3 vías |
| `invoice.approved` | **invoice-bot** → bank-bot | Partida por pagar en la propuesta de pagos |
| `payment.confirmed` | bank-bot → **invoice-bot** | Marca la factura como pagada |
| `invoice.rejected` / `invoice.paid` | **invoice-bot** → email-bot | Aviso al proveedor |
