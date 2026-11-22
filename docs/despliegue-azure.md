# Despliegue en Azure Container Apps

InvoiceBot se despliega con una sola imagen y varios procesos. `deploy/azure/main.bicep` tiene dos modos (`mode`):

**`queue` (por defecto)**: colas en Azure Managed Redis; cada parte escala por separado.

| Recurso | Qué hace | Escala |
| --- | --- | --- |
| Container App `invoicebot-api` | API REST, pantalla de revisión y subida de documentos | Por HTTP (`apiMinReplicas`…3) |
| Container App `invoicebot-worker` | Colas `ingest`, `sunat` y `default` | 0…`workerMaxReplicas` con KEDA según la cola |
| Container App `invoicebot-extractor` | Cola `extract` (OCR e IA, 1 vCPU / 2 GiB) | 0…`workerMaxReplicas` con KEDA, una réplica cada `extractorQueueLength` mensajes |
| Container Apps Job `invoicebot-scheduler` | `invoicebot schedule tick`: tareas programadas y reintentos vencidos | Cron `schedulerCron` (cada 2 min) |
| Container Apps Job `invoicebot-migrate` | `invoicebot db migrate` | Manual, en cada despliegue |
| Azure Managed Redis (`Balanced_B0`) | Colas, límites de llamadas compartidos | TLS (puerto 10000), sin desalojo, AOF, alta disponibilidad |

**`cycle` (clásico, sin Redis)**: un Container Apps Job `invoicebot-cycle` con `invoicebot run` cada 15 minutos y la API. Más barato para pocos documentos.

En los dos modos, el Bicep crea además:

- **Azure SQL Database serverless** (GP_S_Gen5_1, se pausa a los 60 minutos sin uso) con **autenticación solo Microsoft Entra**: la identidad administrada del bot es la administradora del servidor, sin contraseñas que rotar ni guardar.
- **Identidad administrada asignada por el usuario** compartida por todos los procesos (`AZURE_CLIENT_ID` + `INVOICEBOT_DB_AUTH=managed-identity`).
- **Azure Files** montado en `/data` (carpeta de entrada `/data/inbox`, exportaciones `/data/exports`, acceso al correo `/data/app`).
- **Log Analytics**: los logs JSON de todos los contenedores, consultables con KQL.

> Con `mode=queue`, los workers escalan a cero cuando no hay trabajo y el programador corre cada 2 minutos: la base serverless casi no llega a pausarse. Si el volumen es bajo y el costo importa más que la latencia, usa `mode=cycle`.

## Requisitos

- Una suscripción de Azure y la CLI (`az login`).
- La imagen publicada: el workflow **CI** la publica en `ghcr.io/arcgabito/invoice-bot` en cada push a `main`. Haz el paquete **público** en GitHub (Packages → invoice-bot → Package settings → Change visibility) o pasa `registryServer`, `registryUsername` y `registryPassword` (un token con `read:packages`).

## Desplegar

```bash
az group create -n rg-invoicebot -l eastus2
az deployment group create -g rg-invoicebot \
  -f deploy/azure/main.bicep -p deploy/azure/main.parameters.json \
  -p anthropicApiKey="$ANTHROPIC_API_KEY"
az containerapp job start -g rg-invoicebot -n invoicebot-migrate   # crea el esquema
```

La salida muestra `apiUrl` (HTTPS con certificado administrado). Las réplicas no migran el esquema (`INVOICEBOT_AUTO_MIGRATE=false`): mientras falte, `/readyz` responde 503 y la revisión anterior sigue atendiendo.

Crea el primer usuario de la pantalla de revisión con una ejecución manual del Job de migraciones:

```bash
az containerapp job start -g rg-invoicebot -n invoicebot-migrate \
  --image ghcr.io/arcgabito/invoice-bot:latest \
  --command invoicebot --args "user,add,admin,--name,Administrador"
az containerapp job execution list -g rg-invoicebot -n invoicebot-migrate -o table
# La contraseña generada aparece en los logs de esa ejecución (Log Analytics).
```

Y una clave de API para otros sistemas (email-bot, ERP, Power Automate): `invoicebot apikey create email-bot --scope write`.

## Configuración

Las variables de `.env.example` se pasan en `extraEnv` (no secretas) y `extraSecrets` (secretas: se guardan como secretos de Container Apps):

```bash
az deployment group create -g rg-invoicebot -f deploy/azure/main.bicep \
  -p deploy/azure/main.parameters.json \
  -p anthropicApiKey="$ANTHROPIC_API_KEY" \
  -p extraSecrets='{"INVOICEBOT_MAIL_MS_CLIENT_SECRET":"…","INVOICEBOT_SUNAT_CLIENT_SECRET":"…"}' \
  -p extraEnv='[{"name":"INVOICEBOT_MAIL_PROVIDER","value":"graph"},{"name":"INVOICEBOT_MAIL_USER","value":"facturas@empresa.pe"},{"name":"INVOICEBOT_MAIL_MS_TENANT","value":"<id-del-directorio>"},{"name":"INVOICEBOT_MAIL_MS_CLIENT_ID","value":"<id-de-la-app>"}]'
```

- **Programación**: `INVOICEBOT_SCHEDULE_*` en `extraEnv` (cron en la zona de la empresa). El Job `scheduler` solo encola: lo pesado lo hacen los workers.
- **Límites de llamadas**: `INVOICEBOT_AI_RATE_PER_MINUTE` e `INVOICEBOT_SUNAT_RATE_PER_MINUTE` se respetan entre todas las réplicas (el contador está en Redis).
- **Redis propio**: `deployRedis=false` y `redisUrl=rediss://:clave@host:puerto/0`. Los workers quedan con una réplica mínima (las reglas de KEDA se generan solo con Azure Managed Redis).
- **Correo de Microsoft 365**: usa el permiso de aplicación limitado al buzón de facturas (ver [correo-empresa.md](correo-empresa.md)).
- **Carpeta de entrada**: además del correo y de `POST /api/v1/documents`, puedes subir archivos al recurso compartido de Azure Files (`inbox/`), por ejemplo con Azure Storage Explorer o AzCopy.
- **Exportaciones**: con `INVOICEBOT_RUN_EXPORT=xlsx`, la tarea `exports.monthly` deja `exports/invoices-AAAA-MM.xlsx` en Azure Files; también se descargan desde la API (`GET /api/v1/exports`).
- **Copias de seguridad**: Azure SQL las hace solo (restauración a un punto en el tiempo); `db.backup` informa que no aplica.

## Usar un SQL Server existente

Con `deploySql=false` y `existingSqlServer=servidor.database.windows.net`, da acceso a la identidad en la base:

```sql
CREATE USER [invoicebot-id] FROM EXTERNAL PROVIDER;
ALTER ROLE db_owner ADD MEMBER [invoicebot-id];
```

Para un SQL Server con usuario y contraseña, pasa `INVOICEBOT_DB_AUTH=sql` e `INVOICEBOT_DB_USER` en `extraEnv` e `INVOICEBOT_DB_PASSWORD` en `extraSecrets`.

## Despliegue desde GitHub Actions

`.github/workflows/deploy-azure.yml` (manual, *Run workflow*) despliega el Bicep y luego ejecuta el Job `migrate` y espera a que termine. Usa una federación OIDC: crea una aplicación en Entra con una credencial federada para `repo:ArcGabito/invoice-bot:environment:production`, dale el rol *Contributor* en el grupo de recursos y guarda `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID` y `ANTHROPIC_API_KEY` como secretos del entorno `production`.

## Operación

```bash
# Réplicas de cada worker (suben con la cola y bajan a 0 sin trabajo)
az containerapp replica list -g rg-invoicebot -n invoicebot-extractor -o table
# Últimas ejecuciones del programador
az containerapp job execution list -g rg-invoicebot -n invoicebot-scheduler -o table
```

Desde la API (con una clave de API): `GET /api/v1/jobs?status=failed`, `POST /api/v1/jobs/{id}/retry`, `GET /api/v1/schedules` y `GET /metrics` (Prometheus).

Errores en Log Analytics:

```kusto
ContainerAppConsoleLogs_CL
| where ContainerAppName_s startswith "invoicebot"
| extend log = parse_json(Log_s)
| where log.level in ("warning", "error")
| project TimeGenerated, ContainerAppName_s, log.msg, log.error, log.job_id, log.task
| order by TimeGenerated desc
```
