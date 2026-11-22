# Despliegue en un VPS (Docker Compose)

Para un VPS Linux (DigitalOcean, Hetzner, Contabo, un servidor de la oficina…) con Docker y Docker Compose v2.

| Servicio | Qué hace | Perfil |
| --- | --- | --- |
| `api` | API REST y pantalla de revisión en `127.0.0.1:8000` | siempre |
| `worker` | Ciclo RPA continuo (`invoicebot run --watch`) | siempre |
| `sqlserver` | SQL Server 2022 Express en el mismo VPS | `sql` |
| `caddy` | HTTPS automático (Let's Encrypt) delante de la API | `proxy` |

## Instalación

```bash
git clone https://github.com/ArcGabito/invoice-bot.git && cd invoice-bot
cp .env.example .env
nano .env     # ver abajo
mkdir -p inbox exports && sudo chown -R 10001:0 inbox exports
```

Valores mínimos en `.env`:

```dotenv
INVOICEBOT_AI_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
# SQL Server en el mismo VPS (perfil sql)
MSSQL_SA_PASSWORD=Una-Clave-Larga-2026!
INVOICEBOT_DB_AUTH=sql
INVOICEBOT_DB_USER=invoicebot_app
INVOICEBOT_DB_PASSWORD=Otra-Clave-Larga-2026!
INVOICEBOT_DB_TRUST_CERT=true          # certificado autofirmado del contenedor, red interna de Docker
INVOICEBOT_DB_ADMIN_USER=sa            # solo para `db init`; bórralo después
INVOICEBOT_DB_ADMIN_PASSWORD=Una-Clave-Larga-2026!
# HTTPS (perfil proxy)
INVOICEBOT_DOMAIN=facturas.empresa.pe
INVOICEBOT_SERVE_COOKIE_SECURE=true
# Excel del mes en cada ciclo
INVOICEBOT_RUN_EXPORT=xlsx
```

Arranque:

```bash
docker compose --profile sql up -d sqlserver
docker compose run --rm api db init
docker compose run --rm api user add admin --name "Administrador"   # anota la contraseña
docker compose run --rm api apikey create email-bot --scope write  # si otro sistema enviará documentos
docker compose --profile sql --profile proxy up -d
docker compose logs -f worker
```

Abre `https://facturas.empresa.pe/`. El DNS del dominio debe apuntar al VPS y los puertos 80 y 443 deben estar abiertos.

## Con un SQL Server o Azure SQL existente

Omite el perfil `sql` y define `INVOICEBOT_DB_SERVER`, `INVOICEBOT_DB_PORT`, `INVOICEBOT_DB_USER` e `INVOICEBOT_DB_PASSWORD` (y `INVOICEBOT_DB_TRUST_CERT=false` si el servidor tiene un certificado válido, como Azure SQL).

## Carpeta de entrada

`./inbox` del VPS se monta en `/data/inbox`. Los comprobantes pueden llegar por:

- el **correo** (`INVOICEBOT_MAIL_PROVIDER`), que el worker lee en cada ciclo;
- la **API** (`POST /api/v1/documents`), desde email-bot, el ERP o un escáner;
- copia directa (SFTP, `rclone` desde OneDrive/SharePoint/Google Drive, Syncthing).

## Actualizar

```bash
git pull
docker compose pull            # o: docker compose build
docker compose --profile sql --profile proxy up -d
```

Las migraciones se aplican al arrancar, con una copia de seguridad previa en el volumen `sqlserver-backups`.

## Copias de seguridad

El worker hace `BACKUP DATABASE … WITH COPY_ONLY, CHECKSUM` cada `INVOICEBOT_BACKUP_INTERVAL_HOURS` y conserva `INVOICEBOT_BACKUP_RETENTION`. Llévalas fuera del VPS:

```bash
docker run --rm -v invoice-bot_sqlserver-backups:/b -v "$PWD":/out busybox sh -c 'cp /b/*.bak /out/'
```
