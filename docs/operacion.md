# Operación en producción

## Objetivos de servicio (SLO)

| SLO | Objetivo | Cómo se mide |
| --- | --- | --- |
| Disponibilidad de la API | 99,5 % mensual | `up{job="invoicebot"}` y `/readyz` |
| Latencia de consultas | p95 < 500 ms | Trazas de FastAPI (OpenTelemetry) o `deploy/load/k6-api.js` |
| Recepción de documentos | p95 < 1,5 s (solo guarda y encola) | Ídem |
| Comprobante validado | 95 % en menos de 10 min desde que llega | `v_invoice_lifecycle` (`received_at` → revisión) |
| Trabajos fallidos | < 1 % por día | `invoicebot_jobs{status="failed"}` |

## Observabilidad

- **Métricas**: `GET /metrics` (Prometheus, con una clave de API de lectura). Reglas de alerta en `deploy/monitoring/prometheus-alerts.yml`.
- **Trazas**: define `OTEL_EXPORTER_OTLP_ENDPOINT` (y si hace falta `OTEL_EXPORTER_OTLP_HEADERS`). Se instrumentan FastAPI, httpx (IA, SUNAT, webhooks) y SQL Server, y cada trabajo es un span `job <tarea>`. En Azure, envíalas a Application Insights con el colector de OpenTelemetry o Azure Monitor. En un VPS, a Grafana Tempo o Jaeger.
- **Logs**: JSON (`INVOICEBOT_LOG_FORMAT=json`) con `job_id`, `task`, `trace_id` y `span_id`.

## Límites

- `INVOICEBOT_API_RATE_PER_MINUTE` (p. ej. 600): peticiones por minuto por clave de API o persona, compartido entre réplicas con Redis. Responde 429 con `Retry-After`.
- `INVOICEBOT_AI_RATE_PER_MINUTE` e `INVOICEBOT_SUNAT_RATE_PER_MINUTE`: llamadas por minuto entre todos los workers.

## Runbooks

### La API no responde

1. `/healthz` responde y `/readyz` no: mira `detail` (base de datos, esquema, cola).
2. Esquema pendiente: ejecuta el Job o servicio `migrate`.
3. SQL Server o Redis caídos: la API responde 503 en `/readyz` y el balanceador deja de enviarle tráfico. Al volver, todo se recupera solo: los trabajos están en SQL Server y el programador republica lo que quedó sin enviar.

### La cola crece

`invoicebot_job_oldest_wait_seconds` alto:

1. ¿Hay workers? `docker compose ps` o las réplicas del Container App. Con KEDA, revisa que la regla `redis` vea la lista.
2. ¿Están lentos? Revisa las trazas de `job documents.process` (OCR o IA) y los límites de llamadas.
3. Escala: `docker compose up -d --scale extractor=4` o sube `workerMaxReplicas`.

### Mensajes muertos

Son fallos de infraestructura (por ejemplo, SQL Server caído al reclamar el trabajo) que agotaron los reintentos de Dramatiq. El trabajo sigue en `jobs` con su estado: `invoicebot jobs list --status queued` y `invoicebot jobs retry <id>`. Limpia la lista `<namespace>:<cola>.XQ` cuando esté resuelto.

### Trabajos fallidos

`invoicebot jobs list --status failed` → `invoicebot jobs show <id>` (error y parámetros) → corrige la causa → `invoicebot jobs retry <id>`.

### Webhooks

`invoicebot webhook list` muestra las entregas fallidas por suscriptor. `invoicebot webhook events` muestra los eventos y el estado de cada entrega. Una entrega fallida no bloquea nada. Cuando el receptor vuelva, publica de nuevo con `invoicebot enqueue webhooks.sweep` (o espera al programador).

## Copias, recuperación y continuidad

| Componente | Copia | RPO | RTO |
| --- | --- | --- | --- |
| Azure SQL | Restauración a un punto en el tiempo (7 a 35 días) y redundancia de zona opcional | ≤ 5 min | < 1 h |
| SQL Server en VPS | `db.backup` diario (`COPY_ONLY, CHECKSUM`) y copia fuera del VPS | 24 h (o menos si se programa más seguido) | < 2 h |
| Redis | AOF; no es fuente de verdad | — | Minutos (el programador republica) |
| Archivos (`/data`) | Azure Files con *soft delete* y copia del volumen en el VPS | 24 h | < 2 h |

**Prueba de restauración (trimestral)**: restaura la última copia en una base nueva (`invoicebot_restore_test`), apunta un contenedor a ella con `INVOICEBOT_DB_NAME` y ejecuta `invoicebot doctor` e `invoicebot status`. Anota el tiempo real: ese es tu RTO.

## Despliegues sin cortes

- **Container Apps**: cada despliegue crea una revisión nueva. Con `INVOICEBOT_AUTO_MIGRATE=false`, la nueva no queda lista hasta que el Job `migrate` aplica el esquema, y la anterior sigue atendiendo. Para blue/green explícito, usa el modo de varias revisiones con división de tráfico.
- **VPS**: `docker compose pull && docker compose run --rm migrate && docker compose up -d`. Los workers reciben SIGTERM y terminan lo que tienen en curso (hasta 2 minutos). Con dos réplicas de la API tras Caddy no hay corte.

## Seguridad continua

- Dependabot (`.github/dependabot.yml`) para `uv.lock`, la imagen base y las Actions.
- Trivy y SBOM (SPDX) en cada CI. Por ahora es informativo; hazlo bloqueante cuando la línea base esté limpia.
- Lista de verificación (OWASP ASVS nivel 2, resumen):
  - **Autenticación**: SSO con MFA del proveedor, contraseñas con scrypt y bloqueo por intentos.
  - **Sesiones**: cookie `HttpOnly`/`Secure`/`SameSite` con hash en la base, CSRF en cada acción e invalidación al cambiar la contraseña.
  - **Control de acceso**: roles por endpoint y Row-Level Security por empresa.
  - **Validación**: Pydantic en la API, `defusedxml` y límites de tamaño y de archivos.
  - **Criptografía**: TLS hacia SQL Server, Redis con TLS en Azure, HMAC-SHA256 en webhooks y tokens con hash.
  - **Registro**: auditoría con triggers, logs JSON sin secretos y `invoicebot config` que no los muestra.
- Secretos solo en Key Vault (Azure) o en el `.env` del VPS con permisos `600`. Rótalos con `invoicebot apikey revoke` y `invoicebot apikey create`, y con nuevos secretos de webhooks y OIDC.

## Pruebas de carga

`deploy/load/k6-api.js` contra un entorno de pruebas. Referencia de dimensionamiento: 10 000 comprobantes por día por empresa → API con 2 réplicas, `worker` con 1 a 2 y `extractor` con 2 a 4 (según cuántos requieran OCR o IA).
