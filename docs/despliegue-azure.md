# Despliegue en Azure Container Apps

InvoiceBot se despliega como **dos cargas** que usan la misma imagen:

| Recurso | Qué hace | Cuándo corre |
| --- | --- | --- |
| Container App `invoicebot-api` | API REST, pantalla de revisión y subida de documentos | Siempre (o escala a cero) |
| Container Apps Job `invoicebot-cycle` | Ciclo RPA: correo → tipo de cambio → reintentos → escaneo → SUNAT → Excel | Cron (cada 15 min por defecto) |

Además, `deploy/azure/main.bicep` crea:

- **Azure SQL Database serverless** (GP_S_Gen5_1, se pausa a los 60 minutos sin uso) con **autenticación solo Microsoft Entra**: la identidad administrada del bot es la administradora del servidor, sin contraseñas que rotar ni guardar.
- **Identidad administrada asignada por el usuario** compartida por la API y el Job (`AZURE_CLIENT_ID` + `INVOICEBOT_DB_AUTH=managed-identity`).
- **Azure Files** montado en `/data` (carpeta de entrada `/data/inbox`, exportaciones `/data/exports`, acceso al correo `/data/app`).
- **Log Analytics**: los logs JSON de ambos contenedores, consultables con KQL.

## Requisitos

- Una suscripción de Azure y la CLI (`az login`).
- La imagen publicada: el workflow **CI** la publica en `ghcr.io/arcgabito/invoice-bot` en cada push a `main`. Haz el paquete **público** en GitHub (Packages → invoice-bot → Package settings → Change visibility) o pasa `registryServer`, `registryUsername` y `registryPassword` (un token con `read:packages`).

## Desplegar

```bash
az group create -n rg-invoicebot -l eastus2
az deployment group create -g rg-invoicebot \
  -f deploy/azure/main.bicep -p deploy/azure/main.parameters.json \
  -p anthropicApiKey="$ANTHROPIC_API_KEY"
```

La salida muestra `apiUrl` (HTTPS con certificado administrado). Las migraciones se aplican solas al arrancar la API o el primer ciclo.

Crea el primer usuario de la pantalla de revisión con una ejecución manual del Job:

```bash
az containerapp job start -g rg-invoicebot -n invoicebot-cycle \
  --image ghcr.io/arcgabito/invoice-bot:latest \
  --command invoicebot --args "user,add,admin,--name,Administrador"
az containerapp job execution list -g rg-invoicebot -n invoicebot-cycle -o table
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

- **Correo de Microsoft 365**: usa el permiso de aplicación limitado al buzón de facturas (ver [correo-empresa.md](correo-empresa.md)).
- **Carpeta de entrada**: además del correo y de `POST /api/v1/documents`, puedes subir archivos al recurso compartido de Azure Files (`inbox/`), por ejemplo con Azure Storage Explorer o AzCopy.
- **Exportaciones**: con `INVOICEBOT_RUN_EXPORT=xlsx`, cada ciclo deja `exports/invoices-AAAA-MM.xlsx` en Azure Files; también se descargan desde la API (`GET /api/v1/exports`).
- **Copias de seguridad**: Azure SQL las hace solo (restauración a un punto en el tiempo); `invoicebot backup` informa que no aplica.
- **Ciclos parciales**: el Bicep define `INVOICEBOT_RUN_PARTIAL_EXIT_CODE=0`, así una factura con observaciones no marca la ejecución del Job como fallida. Los fallos reales (sin base de datos, configuración errónea) sí salen con código 1 o 2.

## Usar un SQL Server existente

Con `deploySql=false` y `existingSqlServer=servidor.database.windows.net`, da acceso a la identidad en la base:

```sql
CREATE USER [invoicebot-id] FROM EXTERNAL PROVIDER;
ALTER ROLE db_owner ADD MEMBER [invoicebot-id];
```

Para un SQL Server con usuario y contraseña, pasa `INVOICEBOT_DB_AUTH=sql` e `INVOICEBOT_DB_USER` en `extraEnv` e `INVOICEBOT_DB_PASSWORD` en `extraSecrets`.

## Despliegue desde GitHub Actions

`.github/workflows/deploy-azure.yml` (manual, *Run workflow*) usa una federación OIDC: crea una aplicación en Entra con una credencial federada para `repo:ArcGabito/invoice-bot:environment:production`, dale el rol *Contributor* en el grupo de recursos y guarda `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID` y `ANTHROPIC_API_KEY` como secretos del entorno `production`.

## Operación

```bash
# Últimas ejecuciones del ciclo
az containerapp job execution list -g rg-invoicebot -n invoicebot-cycle -o table
# Lanzar un ciclo ahora (o POST /api/v1/runs desde la API)
az containerapp job start -g rg-invoicebot -n invoicebot-cycle
```

Errores en Log Analytics:

```kusto
ContainerAppConsoleLogs_CL
| where ContainerAppName_s startswith "invoicebot"
| extend log = parse_json(Log_s)
| where log.level in ("warning", "error")
| project TimeGenerated, ContainerAppName_s, log.msg, log.error
| order by TimeGenerated desc
```
